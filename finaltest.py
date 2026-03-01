import RPi.GPIO as GPIO
import time
from pathlib import Path
import logging
from ci_core import *
from utils import *
from luma.core.render import canvas
import re
from PIL import ImageFont

CURRENT_LOW_LIMIT = 15
CURRENT_HIGH_LIMIT = 55

logger = logging.getLogger(__name__)

def get_serial_number(text):
    match = re.search(r'Public serial number:\s*(\S+)', text)
    return match.group(1) if match else None

def get_line_with(text, search):
    for line in text.splitlines():
        if search in line:
            return line[line.index(search):]
    return None

class FinalTest(BaochipCIRunner):
    TEST_NAME = 'Dabao Provision'
    def __init__(self, adc, firmware_dir: Path, oled, channel=1, hotplug_callback=None):
        super().__init__(firmware_dir, hotplug_callback)
        self.adc = adc
        self.channel = channel
        self.oled = oled
        self.font = ImageFont.load_default()
        (self.vbus, self.usb) = channel_to_pins(channel)
        # initialize with everything off
        GPIO.output(self.vbus, VBUS_DIS)
        GPIO.output(self.usb, USB_DIS)
        time.sleep(0.5)
        self.errors = []
        self.init_current = None
        self.sn = None
        self.all_files = {
            'boot1' : self.firmware_dir / 'bao1x-boot1.uf2',
            'altboot1' : self.firmware_dir / 'bao1x-alt-boot1.uf2',
            'baremetal' : self.firmware_dir / 'baremetal.uf2',
            'xous' : self.firmware_dir / 'xous.uf2',
            'apps' : self.firmware_dir / 'apps.uf2',
            'loader' : self.firmware_dir / 'loader.uf2',
        }

        for desc, fpath in self.all_files.items():
            logger.info(f"{desc} md5: {get_file_md5(fpath)}")
            logger.info(f"{desc} mtime: {get_file_mtime(fpath)}")

        self.serial_phy = SerialLogger('/dev/ttyS0', '/tmp/phy.log')

        # Connect to local device
        GPIO.setup(PIN_MAPPING['UART_MUX'][0], GPIO.OUT)
        GPIO.output(PIN_MAPPING['UART_MUX'][0], GPIO.HIGH)
        GPIO.output(PIN_MAPPING['LOCAL_PC13_N'][0], GPIO.LOW)

        logging.info("Resetting local Baochip")
        GPIO.output(PIN_MAPPING['LOCAL_RST'][0], GPIO.HIGH)
        time.sleep(0.5)
        GPIO.output(PIN_MAPPING['LOCAL_RST'][0], GPIO.LOW)
        time.sleep(3)
        # togggle boot pin, which should start the program running
        logging.info("Booting local Baochip")
        GPIO.output(PIN_MAPPING['LOCAL_PC13_N'][0], GPIO.HIGH)
        time.sleep(0.3)
        GPIO.output(PIN_MAPPING['LOCAL_PC13_N'][0], GPIO.LOW)
        time.sleep(2)

        hello = self.serial_phy.send_command('test hello')
        if 'DB.TESTER' not in hello:
            logging.error("Can't find local helper")
            self.errors += ["Tester firmware issue"]
            logging.error(hello)
        else:
            logging.info(hello)

    def select_local_serial(self):
        GPIO.output(PIN_MAPPING['UART_MUX'][0], GPIO.HIGH)
    
    def select_dut_serial(self):
        GPIO.output(PIN_MAPPING['UART_MUX'][0], GPIO.LOW)

    def operator_note(self, text, start=None):
        bbox = self.font.getbbox(text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = (self.oled.width - text_width) // 2
        y = (self.oled.height - text_height) // 2

        if start:
            time_text = f"{time.time() - start:0.2f}s"
            time_bbox = self.font.getbbox(time_text)
            time_width = time_bbox[2] - time_bbox[0]
            time_x = (self.oled.width - time_width) // 2

        with canvas(self.oled) as draw:
            draw.rectangle(self.oled.bounding_box, outline="white", fill="white")
            draw.text((x, y), text, font=self.font, fill="black")
            if start:
                draw.text((time_x, y + text_height), time_text, font=self.font, fill="black")

    def run_full_test(self) -> bool:
        logger.info("=" * 80)
        logger.info(f"Starting {self.TEST_NAME}")
        logger.info("=" * 80)

        all_files = self.all_files

        try:
            index = 0            
            self.select_local_serial()
            while True:
                if GPIO.input(PIN_MAPPING['DUT_GND'][0]) == GPIO.LOW:
                    break
                time.sleep(0.1)

            start_time = time.time()
            with canvas(self.oled) as draw:
                draw.text((5, FONT_HEIGHT * 1), "Power on")
            if not self.power_on(with_usb = False):
                raise TestFail
        
            time.sleep(2.0) # wait for first boot
            if not self.report_current():
                raise TestFail

            # Send audit command
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            with canvas(self.oled) as draw:
                draw.text((5, FONT_HEIGHT * 1), "Audit")
            self.select_dut_serial()
            time.sleep(0.2)
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            audit_response = self.serial_phy.send_command('audit')
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            self.results['initial_audit'] = audit_response
            if not audit_response:
                logger.error("No response from audit command")
                self.errors += ["Audit failure"]
                raise TestFail
            self.sn = get_serial_number(audit_response)

            # dabao should always have bootwait enabled coming out of the factory - ensure this is the case
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            self.serial_phy.send_command("bootwait enable", timeout=0.5, expect_response=False)
            self.select_local_serial()
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()

            logging.info("Load test firmware")
            with canvas(self.oled) as draw:
               draw.text((5, FONT_HEIGHT * 1), "Load test program")

            self.power_off()
            time.sleep(2)
            if not self.power_on(with_usb = True):
                raise TestFail

            logging.info("  Find device")
            # Find ACM device
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            acm_path, storage_info = self.device.wait_for_reconnect(
                wait_acm=True, wait_storage=True, timeout=6
            )
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            if not acm_path:
                logger.error("Initial serial DUT not found")
                self.errors += ["Init failure (USB serial)"]
                raise TestFail
            if not storage_info:
                logger.error("Device did not re-enumerate with storage")
                self.errors += ["Init failure (USB storage)"]
            device_path, _ = storage_info
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()

            # Check volume label
            volume_label = self.device.get_volume_label(device_path)
            if volume_label != "BAOCHIP":
                logger.error(f"Expected volume label 'BAOCHIP', got '{volume_label}'")
                self.errors += [f"BAOCHIP missing: {volume_label}"]
                raise TestFail
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            
            # Mount and flash applications
            logging.info("  Copy code")
            mount_point = self.device.mount_device(device_path)
            if not mount_point:
                logger.error("Failed to mount device")
                self.errors += ["Failed to mount device"]
                raise TestFail
                    
            if not self.device.copy_files([self.all_files['baremetal']], mount_point):
                self.device.unmount_device(mount_point)
                logger.error("Failed to copy application files")
                self.errors += ["Couldn't copy test app"]
                raise TestFail
            
            if not self.device.unmount_device(mount_point):
                logger.error("Failed to unmount device")
                self.errors += ["Couldn't unmount device"]
                return TestFail

            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            logging.info("Operator interaction: BOOT")
            operator_timer = time.time()
            while True:
                self.operator_note("==== BOOT ---> ====", start = operator_timer)
                if GPIO.input(PIN_MAPPING['DUT_PC13_N'][0]) == GPIO.LOW:
                    break
                time.sleep(0.1)
            logger.info("boot to baremetal")
            self.oled.clear()
            with canvas(self.oled) as draw:
                draw.text((5, FONT_HEIGHT * 1), "Testing I/O...")
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()

            if False:
                self.results['bio'] = self.device.send_command(acm_path, "bio", timeout=1)
                self.results['bdma'] = self.device.send_command(acm_path, "bdma", timeout=1)
                self.device.send_command(acm_path, "reset", timeout=1, expect_response=False)
                self.device.wait_for_disconnect(acm_path, timeout=5)
            else:
                logger.info("run tests")
                # self.results['io'] = self.device.send_command(acm_path, "dbtest", timeout=8)
                # logger.info(self.results['io'])
                self.results['io_local'] = self.serial_phy.send_command("test dabao", timeout=2)
                logger.info(self.results['io_local'])
                if not 'TEST.PASSING' in self.results['io_local']:
                    self.errors += ["I/O test fail"]
                    details = get_line_with(self.results['io_local'], 'TEST.FAIL')
                    if details:
                        self.errors += [details]
                    raise TestFail
            logger.info("Main test completed successfully")
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()

            # Reset device manually
            logging.info("Operator interaction: RESET")
            operator_timer = time.time()
            while True:
                self.operator_note("#### <--- RESET ####", start = operator_timer)
                if GPIO.input(PIN_MAPPING['DUT_RST'][0]) == GPIO.LOW:
                    break
                time.sleep(0.1)
            logger.info("reset for OS load")
            self.oled.clear()
            with canvas(self.oled) as draw:
                draw.text((5, FONT_HEIGHT * 1), "Loading OS...")

            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            if not self.boot1_verify_main_and_flash_apps([all_files['apps'], all_files['xous'], all_files['loader']]):
                self.errors += ["Xous upload error"]
                self.power_off()
                self.print_results()
                return False
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()
            with canvas(self.oled) as draw:
                draw.text((5, FONT_HEIGHT * 1), "Checking OS...")
            if not self.boot1_final_verification():
                self.errors += ["Final check fail"]
                self.power_off()
                self.print_results()
                return False
            logging.info(f"{index} {time.time() - start_time:.2f}s"); index += 1; start_time = time.time()

            logger.info("=" * 80)
            logger.info(f"    ~~~~~~~~~~ {self.TEST_NAME} Sequence PASSED ~~~~~~~~~~")
            logger.info("=" * 80)
            # self.print_results() # only needed for debugging
            self.power_off()
            return True
            
        except Exception as e:
            logger.error(f"{self.TEST_NAME} failed with exception: {e}", exc_info=True)
            self.errors += [f"{e}"]
            self.report_current()
            self.power_off()
            self.print_results()
            return False

    def power_off(self) -> bool:
        GPIO.output(self.vbus, VBUS_DIS)
        GPIO.output(self.usb, USB_DIS)
        return True

    def power_on(self, with_usb = True) -> bool:
        if with_usb:
            GPIO.output(self.usb, USB_ENA)
        else:
            GPIO.output(self.usb, USB_DIS)
        GPIO.output(self.vbus, VBUS_ENA)
        # fast reading for high current protection
        time.sleep(0.02)
        init_current = self.adc.read_current(self.channel)
        logger.info(f"SC test {init_current:.2f}mA")
        if init_current > 90:
            GPIO.output(self.usb, USB_DIS)
            GPIO.output(self.vbus, VBUS_DIS)
            logger.error("Short circuit likely")
            self.errors += [f"Short circuit {init_current}mA; abort test!"]
            return False
        else:
            return True
    
    def report_current(self) -> bool:
        logger.info(f"Instantaneous current: {self.adc.read_current(self.channel):0.2f} mA")
        # now take a reading after boot stabilization
        init_current = self.adc.read_current(self.channel)
        if self.init_current is None:
            self.init_current = init_current
        logger.info(f"Initial current: {init_current:0.2f} mA")
        if init_current > CURRENT_LOW_LIMIT and init_current < CURRENT_HIGH_LIMIT:
            return True
        elif init_current <= CURRENT_LOW_LIMIT:
            logger.error("Current is too low, is there a device plugged in?")
            self.errors += [f"Current too low: {init_current:0.2f}mA"]
            return False
        else:
            logger.error("Current is too high! Check for shorted/damaged device")
            self.errors += [f"Current too high {init_current:0.2f}mA"]
            return False


    def boot1_flash_alt_boot(self, alt_boot_file) -> bool:
        logger.info("\n--- Flash Alternate Bootloader ---")
        
        # Find storage device
        storage_info = self.device.find_storage_device(timeout=5)
        if not storage_info:
            logger.error("Failed to find storage device")
            return False
        
        device_path, _ = storage_info
        
        # Mount device
        mount_point = self.device.mount_device(device_path)
        if not mount_point:
            logger.error("Failed to mount device")
            return False
        
        # Copy firmware
        if not self.device.copy_files([alt_boot_file], mount_point):
            self.device.unmount_device(mount_point)
            logger.error("Failed to copy firmware")
            return False
        
        # Unmount
        if not self.device.unmount_device(mount_point):
            logger.error("Failed to unmount device")
            return False
        
        # Send boot command
        acm_path = self.device.find_acm_device(timeout=5)
        if not acm_path:
            logger.error("Failed to find ACM device for boot command")
            return False
        
        if self.device.vendor == self.device.VENDOR_ID_OLD:
            # handle case of really old bootloaders where 'boot' command is broken
            input("Press the 'PROG' button now, then hit enter")
        else:
            self.device.send_command(acm_path, "boot", timeout=1, expect_response=False)
        
        # Wait for disconnect
        self.device.wait_for_disconnect(acm_path, timeout=5)
        
        return True
    
    def boot1_verify_alt_and_flash_main(self, main_boot_file) -> bool:
        logger.info("\n--- Verify Alt Boot and Flash Main Boot ---")
        
        # Wait for re-enumeration
        acm_path, storage_info = self.device.wait_for_reconnect(
            wait_acm=True, wait_storage=True, timeout=5
        )
        
        if not storage_info:
            logger.error("Device did not re-enumerate with storage")
            return False
        
        device_path, _ = storage_info
        
        # Check volume label
        volume_label = self.device.get_volume_label(device_path)
        if volume_label != "ALTCHIP":
            logger.error(f"Expected volume label 'ALTCHIP', got '{volume_label}'")
            return False
        
        # Mount and flash main boot
        mount_point = self.device.mount_device(device_path)
        if not mount_point:
            logger.error("Failed to mount device")
            return False
        
        if not self.device.copy_files([main_boot_file], mount_point):
            self.device.unmount_device(mount_point)
            logger.error("Failed to copy firmware")
            return False
        
        if not self.device.unmount_device(mount_point):
            logger.error("Failed to unmount device")
            return False
        
        # Send boot command
        if not acm_path:
            acm_path = self.device.find_acm_device(timeout=5)
        
        if not acm_path:
            logger.error("Failed to find ACM device for boot command")
            return False
        
        self.device.send_command(acm_path, "boot", timeout=1, expect_response=False)
        self.device.wait_for_disconnect(acm_path, timeout=5)
        
        return True
    
    def boot1_verify_main_and_flash_apps(self, app_files: List) -> bool:
        logger.info("\n--- Verify Main Boot and Flash Applications ---")
        
        # Wait for re-enumeration
        acm_path, storage_info = self.device.wait_for_reconnect(
            wait_acm=True, wait_storage=True, timeout=10
        )
        
        if not storage_info:
            logger.error("Device did not re-enumerate with storage")
            return False
        
        device_path, _ = storage_info
        
        # Check volume label
        volume_label = self.device.get_volume_label(device_path)
        if volume_label != "BAOCHIP":
            logger.error(f"Expected volume label 'BAOCHIP', got '{volume_label}'")
            return False
        
        # Mount and flash applications
        mount_point = self.device.mount_device(device_path)
        if not mount_point:
            logger.error("Failed to mount device")
            return False
                
        if not self.device.copy_files(app_files, mount_point):
            self.device.unmount_device(mount_point)
            logger.error("Failed to copy application files")
            return False
        
        if not self.device.unmount_device(mount_point):
            logger.error("Failed to unmount device")
            return False
        
        # Send boot command
        self.device.send_command(acm_path, "boot", timeout=1, expect_response=False)
        self.device.wait_for_disconnect(acm_path, timeout=5)
        
        return True
    
    def boot1_final_verification(self) -> bool:
        """Wait for final boot (ACM only), run ver xous"""
        logger.info("\n--- Final Verification ---")
        
        # Wait for ACM only (no storage in final state)
        acm_path, _ = self.device.wait_for_reconnect(
            wait_acm=True, wait_storage=False, timeout=5
        )
        
        if not acm_path:
            logger.error("Device did not re-enumerate with ACM interface")
            return False
        
        # Run version command
        time.sleep(1) # just a little time for xous to boot
        ver_response = self.device.send_command(acm_path, "ver xous", timeout=0.5)
        self.results['final_version'] = ver_response
        
        if not ver_response:
            logger.error("No response from ver xous command")
            return False
        
        return True