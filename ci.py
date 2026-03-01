#!/usr/bin/env python3
"""
GPIO setup script for USB and VBUS control pins
Configures pins as outputs with default state LOW (0)
"""

# FTFMEXWR - duart serial

from ads1115 import ADS1115
import argparse
from pathlib import Path
import logging
import sys
import shutil

from ci_core import *
from dabao_provision import *
from bdma_fuzz import *
from finaltest import *

from luma.core.render import canvas
from luma.core.interface.serial import bitbang
from luma.oled.device import ssd1322
import luma.oled.device

VERSION = "03/01/26"
oled = None

def main(adc):
    parser = argparse.ArgumentParser(description="CI automation script")
    parser.add_argument(
        "--run-test", help="Which test to run",
        choices=[
            'final-test',
            'dabao-provision',
            'currents',
            'bdma-fuzz',
        ]
    )
    parser.add_argument(
        "--port", type=int, help="Which port to run the test on", choices=[1, 2, 3, 4], default = 1
    )
    parser.add_argument(
        '--firmware-dir', type=Path, help='Directory containing firmware files (.uf2)', default='./images/'
    )
    parser.add_argument(
        "--logfile", type=Path, help="File for output logs", default = 'baochip_ci.log'
    )
    parser.add_argument(
        "--overwrite", action="store_true", help = "When specified, overwrite the log file instead of appending"
    )
    parser.add_argument(
        "--duart-log", type=Path, help="File for duart log", default = Path("../logs/duart.log")
    )
    parser.add_argument(
        "--console-log", type=Path, help="File for console log", default = Path("../logs/console.log")
    )

    args = parser.parse_args()

    # Configure logging
    if args.overwrite:
        mode = 'w'
    else:
        mode = 'a'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(args.logfile, mode=mode),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger = logging.getLogger(__name__)

    setup_gpio()

    with canvas(oled) as draw:
        draw.text((0, FONT_HEIGHT * 0), f"Dabao tester ({VERSION}) starting...")
    
    GPIO.output(PIN_MAPPING['LOCAL_PC13_N'][0], GPIO.LOW)
    time.sleep(1)
    GPIO.output(PIN_MAPPING['LOCAL_PC13_N'][0], GPIO.HIGH)

    # board silkscreen is 1-4, but channels are 0-3
    channel = args.port - 1

    if args.run_test == 'currents':
        test_currents(adc, channel, with_usb=False)
    elif args.run_test == 'dabao-provision':
        if not args.firmware_dir.exists():
            logger.error(f"Firmware directory not found: {args.firmware_dir}")
            sys.exit(1)
        test = DabaoProvision(adc, args.firmware_dir, channel)
        return test.run_full_test()
    elif args.run_test == "bdma-fuzz":
        test = BdmaFuzz(adc, args.firmware_dir, channel)
        return test.run_full_test()
    elif args.run_test == 'final-test':
        test = FinalTest(adc, args.firmware_dir, oled, channel)
        if len(test.errors) != 0:
            with canvas(oled) as draw:
                draw.text((0, FONT_HEIGHT * 0), f"Dabao tester ({VERSION}) INTERNAL ERROR")
                draw.text((0, FONT_HEIGHT * 1), f"Contact bunnie@baochip.com for support")
            while True:
                time.sleep(1)

        while True:
            with canvas(oled) as draw:
                draw.text((0, FONT_HEIGHT * 0), f"Dabao tester ({VERSION}) up!")
                draw.text((0, FONT_HEIGHT * 2), "Insert device to start test...")
            
            start_time = time.time()
            test.run_full_test()

            # wait for device to be removed
            fill = "white"
            while True:
                if len(test.errors) != 0:
                    with canvas(oled) as draw:
                        if fill == "black":
                            draw.rectangle(oled.bounding_box, outline="white", fill="white")
                        if test.sn:
                            draw.text((0, FONT_HEIGHT * 0), f"xxx FAIL FAIL FAIL ({test.sn}) ({time.time() - start_time:0.2f}s) xxx", fill=fill)
                        else:
                            draw.text((0, FONT_HEIGHT * 0), f"xxx FAIL FAIL FAIL ({time.time() - start_time:0.2f}s) xxx", fill=fill)
                        for i, err in enumerate(test.errors):
                            draw.text((0, FONT_HEIGHT * (i+1)), err, fill=fill)
                else:
                    with canvas(oled) as draw:
                        if fill == "black":
                            draw.rectangle(oled.bounding_box, outline="white", fill="white")
                        draw.text((0, FONT_HEIGHT * 1), f"~~~~~ PASS ({test.sn}/{test.init_current:.2f}mA) ~~~~~", fill=fill)
                        ver = next((l for l in test.results['final_version'].splitlines() if "Xous version" in l), None)
                        if ver:
                            draw.text((0, FONT_HEIGHT * 3), f"{ver}", fill=fill)
                        draw.text((0, FONT_HEIGHT*4), f"Elapsed: {time.time() - start_time:0.2f}s", fill=fill)

                if GPIO.input(PIN_MAPPING['DUT_GND'][0]) == GPIO.HIGH:
                    break
                time.sleep(0.5)
                if fill == "white":
                    fill = "black"
                else:
                    fill = "white"
            test.errors = []
            test.results = {}
            test.init_current = None
            test.sn = None

    else:
        print("No test selected")
    return False

if __name__ == "__main__":
    cleaned_up = False
    try:
        adc = ADS1115()
        oled = ssd1322(bitbang(SCLK=8, SDA=25, CE=7, DC=1, RST=12))        
        if main(adc):
            exit(0)
    except KeyboardInterrupt:
        cleanup()
        adc.close()
        cleaned_up = True
    if not cleaned_up:
        cleanup()
        adc.close()
    exit(1)