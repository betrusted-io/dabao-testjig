import RPi.GPIO as GPIO
import time
from pathlib import Path
import logging
from ci_core import *
from utils import *

logger = logging.getLogger(__name__)

class DabaoProvision(BaochipCIRunner):
    TEST_NAME = 'Dabao Provision'
    def __init__(self, adc, firmware_dir: Path, channel=1, hotplug_callback=None):
        super().__init__(firmware_dir, hotplug_callback)
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
            'boot1' : self.firmware_dir / 'bootloader/bao1x-boot1.uf2',
            'altboot1' : self.firmware_dir / 'bootloader/bao1x-alt-boot1.uf2',
            'baremetal' : self.firmware_dir / 'baremetal/baremetal.uf2',
            'xous' : self.firmware_dir / 'dabao/xous.uf2',
            'apps' : self.firmware_dir / 'dabao/apps.uf2',
            'loader' : self.firmware_dir / 'dabao/loader.uf2',
        }

        for desc, fpath in all_files.items():
            logger.info(f"{desc} md5: {get_file_md5(fpath)}")
            logger.info(f"{desc} mtime: {get_file_mtime(fpath)}")

        try:
            if not self.power_on():
                self.power_off()
                return False
            if not self.boot1_audit():
                self.power_off()
                return False
            if not self.boot1_flash_alt_boot(all_files['altboot1']):
                self.power_off()
                return False
            if not self.boot1_verify_alt_and_flash_main(all_files['boot1']):
                self.power_off()
                return False
            if True: # optional BIO test - just done during an initial shakedown
                if not self.boot1_test_bio([all_files['baremetal']]):
                    self.power_off()
                    return False
            if not self.boot1_verify_main_and_flash_apps([all_files['apps'], all_files['xous'], all_files['loader']]):
                self.power_off()
                return False
            if not self.boot1_final_verification():
                self.power_off()
                return False

            logger.info("=" * 80)
            logger.info(f"    ~~~~~~~~~~ {self.TEST_NAME} Sequence PASSED ~~~~~~~~~~")
            logger.info("=" * 80)
            self.report_current()
            self.print_results()
            self.power_off()
            return True
            
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

    def boot1_audit(self) -> bool:
        """Step 1: Connect to ACM, run audit, check volume label"""
        logger.info("\n--- STEP 1: Initial Audit ---")
        
        # Find ACM device
        acm_path = self.device.find_acm_device(timeout=15)
        if not acm_path:
            logger.error("Failed to find ACM device")
            return False
        
        # Send audit command
        time.sleep(2.0)
        audit_response = self.device.send_command(acm_path, "audit", timeout=1)
        self.results['initial_audit'] = audit_response
        
        if not audit_response:
            logger.error("No response from audit command")
            return False
        
        # Find storage device and check label
        storage_info = self.device.find_storage_device(timeout=5)
        if not storage_info:
            logger.error("Failed to find storage device")
            return False
        
        device_path, _ = storage_info
        volume_label = self.device.get_volume_label(device_path)
        
        if volume_label != "BAOCHIP":
            logger.error(f"Expected volume label 'BAOCHIP', got '{volume_label}'")
            return False
        
        logger.info("Step 1 completed successfully")
        return True

    def boot1_flash_alt_boot(self, alt_boot_file) -> bool:
        """Step 2-3: Mount, copy alt boot firmware, unmount, boot"""
        logger.info("\n--- STEP 2-3: Flash Alternate Bootloader ---")
        
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
        
        logger.info("Step 2-3 completed successfully")
        return True
    
    def boot1_verify_alt_and_flash_main(self, main_boot_file) -> bool:
        """Step 4-5: Wait for re-enumeration, verify ALTCHIP label, flash main boot"""
        logger.info("\n--- STEP 4-5: Verify Alt Boot and Flash Main Boot ---")
        
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
        
        logger.info("Step 4-5 completed successfully")
        return True
    
    def boot1_test_bio(self, app_files: List) -> bool:
        logger.info("\n-- BONUS: test BIO")
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
        if not acm_path:
            acm_path = self.device.find_acm_device(timeout=5)
        
        if not acm_path:
            logger.error("Failed to find ACM device for boot command")
            return False
        
        self.device.send_command(acm_path, "boot", timeout=1, expect_response=False)
        self.device.wait_for_disconnect(acm_path, timeout=5)

        # Wait for re-enumeration, then issue test commands
        acm_path, _ = self.device.wait_for_reconnect(
            wait_acm=True, wait_storage=False, timeout=10
        )
        if acm_path:
            self.results['bio'] = self.device.send_command(acm_path, "bio", timeout=1)
            self.results['bdma'] = self.device.send_command(acm_path, "bdma", timeout=1)
            self.device.send_command(acm_path, "reset", timeout=1, expect_response=False)
            self.device.wait_for_disconnect(acm_path, timeout=5)
        
        logger.info("Bonus steps completed successfully")
        return True        

    def boot1_verify_main_and_flash_apps(self, app_files: List) -> bool:
        """Step 6-7: Verify BAOCHIP label, run audit, flash applications"""
        logger.info("\n--- STEP 6-7: Verify Main Boot and Flash Applications ---")
        
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
        
        # Run audit
        if not acm_path:
            acm_path = self.device.find_acm_device(timeout=5)
        
        if acm_path:
            audit_response = self.device.send_command(acm_path, "audit", timeout=1)
            self.results['main_boot_audit'] = audit_response
        
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
        if not acm_path:
            acm_path = self.device.find_acm_device(timeout=5)
        
        if not acm_path:
            logger.error("Failed to find ACM device for boot command")
            return False
        
        self.device.send_command(acm_path, "boot", timeout=1, expect_response=False)
        self.device.wait_for_disconnect(acm_path, timeout=5)
        
        logger.info("Step 6-7 completed successfully")
        return True
    
    def boot1_final_verification(self) -> bool:
        """Step 8: Wait for final boot (ACM only), run ver xous"""
        logger.info("\n--- STEP 8: Final Verification ---")
        
        # Wait for ACM only (no storage in final state)
        acm_path, _ = self.device.wait_for_reconnect(
            wait_acm=True, wait_storage=False, timeout=10
        )
        
        if not acm_path:
            logger.error("Device did not re-enumerate with ACM interface")
            return False
        
        # Run version command
        time.sleep(5)
        ver_response = self.device.send_command(acm_path, "ver xous", timeout=2)
        self.results['final_version'] = ver_response
        
        if not ver_response:
            logger.error("No response from ver xous command")
            return False
        
        logger.info("Step 8 completed successfully")
        return True