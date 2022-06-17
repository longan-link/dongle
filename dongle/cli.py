import click

from dongle import utils


@click.group()
def run():
    pass


@click.command()
@click.option(
    "--firmware",
    "-f",
    help="firmware path ready be flash.)",
)
def boot(firmware: str):
    utils.boot(firmware)


@click.command()
@click.argument("firmware")
@click.option(
    "--port",
    "-p",
    default="/dev/ttyUSB0",
    help="port of flash dev.(defalut: /dev/ttyUSB0)",
)
def flash(firmware: str, port: str):
    utils.flash_firmware(port=port, firmware_path=firmware)


run.add_command(boot)
run.add_command(flash)
