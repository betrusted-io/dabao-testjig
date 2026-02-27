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

def main(adc):
    parser = argparse.ArgumentParser(description="CI automation script")
    parser.add_argument(
        "--run-test", help="Which test to run",
        choices=[
            'dabao-provision',
            'currents',
            'bdma-fuzz',
        ]
    )
    parser.add_argument(
        "--port", type=int, help="Which port to run the test on", choices=[1, 2, 3, 4], default = 1
    )
    parser.add_argument(
        '--firmware-dir', type=Path, help='Directory containing firmware files (.uf2)'
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
    else:
        print("No test selected")
    return False

if __name__ == "__main__":
    cleaned_up = False
    try:
        adc = ADS1115()
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