import os
import struct
import sys
import time
import traceback

from loguru import logger

from dongle.cc2538_bsl import (
    CommandInterface,
    CmdException,
    FirmwareFile,
    mdebug,
    CHIP_ID_STRS,
    CC26xx,
    CC2538,
    parse_page_address_range,
    parse_ieee_address,
    QUIET,
)

mode = rstpin = bslpin = None
try:
    import Jetson.GPIO as GPIO

    if GPIO.RPI_INFO["TYPE"] != "Jetson Nano":
        logger.info("rpi")
        mode = GPIO.BCM
        rstpin, bslpin = 4, 22
    else:
        logger.info("nano")
        mode = GPIO.BOARD
        rstpin, bslpin = 7, 15
except (ImportError, RuntimeError, ModuleNotFoundError) as e:
    # logger.debug(e)
    import fake_rpigpio.utils

    fake_rpigpio.utils.install()
    from fake_rpigpio.RPi import GPIO


def get_dev():
    dev_type = getattr(GPIO, "RPI_INFO", {}).get("TYPE", "unknow")
    devs = {"Pi 3 Model B": "ttyS0", "Jetson Nano": "ttyTHS1"}
    dev = f"/dev/ttyUSB0"

    if dev_type in devs.keys() and not os.path.exists(dev):
        dev = f"/dev/{devs[dev_type]}"

    return dev


def boot(firmware=None):
    """
    dongle startup.
    bslpin - 1. LOW: download mode; 2. HIGH: run mode;
    rstpin - LOW -> HIGH: read bslpin status
    """
    dev = get_dev()
    logger.info(f"dongle dev: {dev}")
    if "ttyUSB0" not in dev:
        # GPIO control, enter flash mode and flash firmware, or reset to run mode
        GPIO.setwarnings(False)
        GPIO.setmode(mode)
        GPIO.setup([bslpin, rstpin], GPIO.OUT, initial=GPIO.HIGH)

        try:
            if firmware:
                logger.info("dongle mode: flash")

                # enter flash mode
                GPIO.output([bslpin, rstpin], GPIO.LOW)
                time.sleep(0.3)  # sleep for about 300ms
                GPIO.output(rstpin, GPIO.HIGH)
                time.sleep(0.3)

                flash_firmware(port=dev, firmware_path=firmware)
            else:
                logger.info("dongle mode: run")

            # reset to run mode
            GPIO.output([bslpin, rstpin], (GPIO.HIGH, GPIO.LOW))
            time.sleep(0.3)  # sleep for about 300ms
            GPIO.output(rstpin, GPIO.HIGH)
            time.sleep(0.3)  # sleep for about 300ms

        except KeyboardInterrupt:
            pass


def flash_firmware(port: str, firmware_path: str, exit_ = True):
    """flash firmware

    Arguments:
        port {str} -- dev
        firmware_path {str} -- firmware file path

    Raises:
        CmdException: _description_
        CmdException: _description_
        CmdException: _description_
        CmdException: _description_
        CmdException: _description_
        Exception: _description_
        CmdException: _description_
    """
    logger.info("flash firmware.")
    conf = {
        "port": port,  # dev
        "baud": 115200,
        "force_speed": 0,
        "address": None,
        "force": 0,
        "erase": 1,
        "write": 1,
        "erase_page": 0,
        "verify": 1,
        "read": 0,
        "len": 0x80000,
        "fname": "",
        "ieee_address": 0,
        "bootloader_active_high": False,
        "bootloader_invert_lines": False,
        "disable-bootloader": 0,
    }
    try:
        cmd = CommandInterface()
        cmd.open(conf["port"], conf["baud"])
        cmd.invoke_bootloader(
            conf["bootloader_active_high"], conf["bootloader_invert_lines"]
        )

        mdebug(5, f"Opening port {conf['port']}, baud {conf['baud']}")
        if conf["write"] or conf["verify"]:
            mdebug(5, "Reading data from %s" % firmware_path)
            firmware = FirmwareFile(firmware_path)

            mdebug(5, "Connecting to target...")

            if not cmd.sendSynch():
                raise CmdException(
                    "Can't connect to target. Ensure boot loader "
                    "is started. (no answer on synch sequence)"
                )

        # if (cmd.cmdPing() != 1):
        #     raise CmdException("Can't connect to target. Ensure boot loader "
        #                        "is started. (no answer on ping command)")

        chip_id = cmd.cmdGetChipId()
        chip_id_str = CHIP_ID_STRS.get(chip_id, None)

        if chip_id_str is None:
            mdebug(10, "    Unrecognized chip ID. Trying CC13xx/CC26xx")
            device = CC26xx(cmd)
        else:
            mdebug(10, "    Target id 0x%x, %s" % (chip_id, chip_id_str))
            device = CC2538(cmd)

        # Choose a good default address unless the user specified -a
        if conf["address"] is None:
            conf["address"] = device.flash_start_addr

        if conf["force_speed"] != 1 and device.has_cmd_set_xosc:
            if cmd.cmdSetXOsc():  # switch to external clock source
                cmd.close()
                conf["baud"] = 1000000
                cmd.open(conf["port"], conf["baud"])
                mdebug(
                    6,
                    "Opening port %(port)s, baud %(baud)d"
                    % {"port": conf["port"], "baud": conf["baud"]},
                )
                mdebug(6, "Reconnecting to target at higher speed...")
                if cmd.sendSynch() != 1:
                    raise CmdException(
                        "Can't connect to target after clock "
                        "source switch. (Check external "
                        "crystal)"
                    )
            else:
                raise CmdException(
                    "Can't switch target to external clock "
                    "source. (Try forcing speed)"
                )

        if conf["erase"]:
            mdebug(5, "    Performing mass erase")
            if device.erase():
                mdebug(5, "    Erase done")
            else:
                raise CmdException("Erase failed")

        if conf["erase_page"]:
            erase_range = parse_page_address_range(device, conf["erase_page"])
            mdebug(
                5, "Erasing %d bytes at addres 0x%x" % (erase_range[1], erase_range[0])
            )
            cmd.cmdEraseMemory(erase_range[0], erase_range[1])
            mdebug(5, "    Partial erase done                  ")

        if conf["write"]:
            # TODO: check if boot loader back-door is open, need to read
            #       flash size first to get address
            if cmd.writeMemory(conf["address"], firmware.bytes):
                mdebug(5, "    Write done                                ")
            else:
                raise CmdException("Write failed                       ")

        if conf["verify"]:
            mdebug(5, "Verifying by comparing CRC32 calculations.")

            crc_local = firmware.crc32()
            # CRC of target will change according to length input file
            crc_target = device.crc(conf["address"], len(firmware.bytes))

            if crc_local == crc_target:
                mdebug(5, "    Verified (match: 0x%08x)" % crc_local)
            else:
                cmd.cmdReset()
                raise Exception(
                    "NO CRC32 match: Local = 0x%x, "
                    "Target = 0x%x" % (crc_local, crc_target)
                )

        if conf["ieee_address"] != 0:
            ieee_addr = parse_ieee_address(conf["ieee_address"])
            mdebug(
                5,
                "Setting IEEE address to %s"
                % (":".join(["%02x" % b for b in struct.pack(">Q", ieee_addr)])),
            )
            ieee_addr_bytes = struct.pack("<Q", ieee_addr)

            if cmd.writeMemory(device.addr_ieee_address_secondary, ieee_addr_bytes):
                mdebug(5, "    " "Set address done                                ")
            else:
                raise CmdException("Set address failed                       ")

        if conf["read"]:
            length = conf["len"]

            # Round up to a 4-byte boundary
            length = (length + 3) & ~0x03

            mdebug(
                5,
                "Reading %s bytes starting at address 0x%x" % (length, conf["address"]),
            )
            with open(firmware_path, "wb") as f:
                for i in range(0, length >> 2):
                    # reading 4 bytes at a time
                    rdata = device.read_memory(conf["address"] + (i * 4))
                    mdebug(
                        5,
                        " 0x%x: 0x%02x%02x%02x%02x"
                        % (
                            conf["address"] + (i * 4),
                            rdata[0],
                            rdata[1],
                            rdata[2],
                            rdata[3],
                        ),
                        "\r",
                    )
                    f.write(rdata)
                f.close()
            mdebug(5, "    Read done                                ")

        if conf["disable-bootloader"]:
            device.disable_bootloader()

        cmd.cmdReset()

    except Exception as err:
        if QUIET >= 10:
            traceback.print_exc()
        if exit_:
            exit("ERROR: %s" % str(err))


if __name__ == "__main__":
    print(get_dev())
    flash_firmware(
        port="/dev/tty.usbserial-0001",
        firmware_path="./hexs/CC1352P2_CC2652P_launchpad_coordinator_20210120.hex",
    )
    # if len(sys.argv) == 1:
    #     boot()
    # elif sys.argv[1] == "bf":
    #     boot(flash=True)
    # elif sys.argv[1] == "f":
    #     flash_firmware()
