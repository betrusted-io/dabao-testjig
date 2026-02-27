import RPi.GPIO as GPIO
import time
from pathlib import Path
import logging
from ci_core import *
from utils import *

logger = logging.getLogger(__name__)

class BdmaFuzz(BaochipCIRunner):
    TEST_NAME = 'BDMA Fuzzer'
    def __init__(self, adc, firmware_dir: Path, channel=1):
        super().__init__(firmware_dir, None)
        self.adc = adc
        self.channel = channel
        (self.vbus, self.usb) = channel_to_pins(channel)
        # initialize with everything off
        GPIO.output(self.vbus, VBUS_DIS)
        GPIO.output(self.usb, USB_DIS)
        time.sleep(0.5)

    def run_full_test(self) -> bool:
        logger.info("=" * 80)
        logger.info(f"Starting {self.TEST_NAME}")
        logger.info("=" * 80)
        
        all_files = {
            'baremetal' : self.firmware_dir / 'baremetal/baremetal.uf2',
        }

        for desc, fpath in all_files.items():
            logger.info(f"{desc} md5: {get_file_md5(fpath)}")
            logger.info(f"{desc} mtime: {get_file_mtime(fpath)}")

        try:
            if not self.power_on():
                self.power_off()
                return False

            if not self.load_and_boot([all_files['baremetal']]):
                self.power_off()
                return False
            
            found_fail = False
            i = 0
            fail_iter = []
            while True:
                logging.info(f"Iter {i + 1}")
                # Wait for re-enumeration, then issue test commands
                acm_path, _ = self.device.wait_for_reconnect(
                    wait_acm=True, wait_storage=False, timeout=10
                )
                if acm_path:
                    self.results['bio' + str(i)] = self.device.send_command(acm_path, "bio", timeout=1)
                    self.results['bdma' + str(i)] = self.device.send_command(acm_path, "bdma", timeout=1)
                    if "~~BDMAFAIL~~" in self.results['bdma' + str(i)]:
                        found_fail = True
                        logging.info("FAIL")
                        logging.info(self.results['bdma' + str(i)])
                        fail_iter += [i]
                    else:
                        logging.info("PASS")
                    if found_fail:
                        # reset loop, don't power cycle
                        self.device.send_command(acm_path, "reset", timeout=1, expect_response=False)
                        self.device.wait_for_disconnect(acm_path, timeout=5)
                    else:
                        # power cycle to find a fail
                        self.power_off()
                        time.sleep(5)
                        self.power_on()

                # Wait for re-enumeration
                acm_path, storage_info = self.device.wait_for_reconnect(
                    wait_acm=True, wait_storage=True, timeout=10
                )
                self.device.send_command(acm_path, "boot", timeout=1, expect_response=False)
                self.device.wait_for_disconnect(acm_path, timeout=5)
                i += 1
                if i > 25 and not found_fail:
                    logger.info("Abort due to inability to find a failure")
                    break
                if i > 150 and found_fail:
                    logger.info("Abort due to test length limit")
                    break

            logger.info(f"Fail at iters: {fail_iter}")
            logger.info("=" * 80)
            logger.info(f"    ~~~~~~~~~~ {self.TEST_NAME} Sequence PASSED ~~~~~~~~~~")
            logger.info("=" * 80)
            self.report_current()
            self.print_results()
            self.power_off()
            if not found_fail:
                return True
            else:
                return False
            
        except Exception as e:
            logger.error(f"{self.TEST_NAME} failed with exception: {e}", exc_info=True)
            self.power_off()
            return False

    def power_off(self) -> bool:
        GPIO.output(self.vbus, VBUS_DIS)
        return True

    def power_on(self) -> bool:
        GPIO.output(self.usb, USB_ENA)
        GPIO.output(self.vbus, VBUS_ENA)
        time.sleep(2.0)
        init_current = self.adc.read_current(self.channel)
        logger.info(f"Initial current: {init_current:0.2f} mA")
        if init_current > 20 and init_current < 70:
            return True
        elif init_current <= 20:
            logger.error("Current is too low, is there a device plugged in?")
            return False
        else:
            logger.error("Current is too high! Check for shorted/damaged device")
            return False
    
    def report_current(self):
        logger.info(f"Instantaneous current: {self.adc.read_current(self.channel):0.2f} mA")
