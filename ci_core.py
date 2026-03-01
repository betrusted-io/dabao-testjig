import RPi.GPIO as GPIO
from ads1115 import ADS1115
import argparse
import time
from pathlib import Path
import logging
import sys
from typing import Optional, Tuple, List
import subprocess
import pyudev
import serial
import hashlib

logger = logging.getLogger(__name__)

class TimeoutException(Exception):
    pass

class PowerException(Exception):
    pass

class CommException(Exception):
    pass

class LoadException(Exception):
    pass

class TestFail(Exception):
    pass

class InternalError(Exception):
    pass

# GPIO pin mappings from schematic
# Format: 'signal_name': GPIO_number
# Note: Connector is flipped horizontally, so odd pins → even, even → odd
PIN_MAPPING = {
    'USB_1': (16, 'out', GPIO.HIGH), # disabled
    'VBUS_1': (26, 'out', GPIO.LOW),
    'BUTTON_1': (13, 'in', None),
    'BUTTON_2': (19, 'in', None),
    'UART_MUX': (6, 'out', GPIO.LOW),
    # 'OLED_RES': (12, 'out', GPIO.LOW),
    'DUT_PC13_N': (5, 'in', None),
    'DUT_RST': (0, 'in', None),
    # 'OLED_DC': (1, 'out', GPIO.LOW),
    # 'OLED_CS_N': (7, 'out', GPIO.HIGH),
    'DUT_EN_N': (11, 'out', GPIO.LOW),
    # 'OLED_SCK': (8, 'out', GPIO.LOW),
    'LOCAL_PC13_N': (9, 'out', GPIO.LOW),
    'LOCAL_RST': (21, 'out', GPIO.LOW),
    # 'OLED_MOSI': (25, 'out', GPIO.LOW),
    'DUT_GND': (10, 'in', None), # used to auto-trigger the tester

}

VBUS_ENA = GPIO.HIGH
VBUS_DIS = GPIO.LOW
USB_ENA = GPIO.LOW
USB_DIS = GPIO.HIGH

FONT_HEIGHT=12

def setup_gpio():
    """Initialize GPIO pins as outputs with default LOW state"""
    
    # Use BCM GPIO numbering
    GPIO.setmode(GPIO.BCM)
    
    # Configure each pin as output with initial state LOW
    for signal_name, (gpio_num, dir, init) in PIN_MAPPING.items():
        if dir == 'out':
            GPIO.setup(gpio_num, GPIO.OUT, initial=init)
        else:
            GPIO.setup(gpio_num, GPIO.IN)
        print(f"Configured {signal_name} (GPIO{gpio_num}) as {dir}, initial state: {init}")
    
    print("\nAll pins configured successfully!")

def cleanup():
    """Clean up GPIO resources"""
    GPIO.cleanup()
    print("GPIO cleanup complete")

def channel_to_pins(channel):
    if channel == 0:
        return (PIN_MAPPING['VBUS_1'][0], PIN_MAPPING['USB_1'][0])
    return None

def test_currents(adc, channel=1, with_usb=False):
    (vbus, usb) = channel_to_pins(channel)

    GPIO.output(vbus, GPIO.HIGH)
    if with_usb:
        GPIO.output(usb, USB_ENA)
    else:
        GPIO.output(usb, USB_DIS)

    print("\nPins are now set. Press Ctrl+C to cleanup and exit...")
    # Keep the script running
    while True:
        raw_value = adc.read_adc(channel)
        current = adc.read_current(channel)
        print(f"Channel {channel}: Raw={raw_value:6d}, Current={current:.2f}mA")

def md5sum(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

class BaochipDevice:
    """Represents and manages a Baochip device through its test lifecycle"""
    VENDOR_ID_OLD = "1209"
    PRODUCT_ID_OLD = "3613"

    VENDOR_ID = "1d50"
    PRODUCT_ID = "6196"
    PRODUCT_ID_XOUS = "6197"

    PRODUCT_ID_LOCAL = "6666"
    PRODUCT_ID_LOCAL_XOUS = "6667"
    DEVICE_NAME = "Baochip-1x"
    
    def __init__(self, serial_number: Optional[str] = None, hotplug_callback=None):
        """
        Initialize device manager
        
        Args:
            serial_number: Specific device serial number to target (optional)
            hotplug_callback: Optional hotplug() function for device reset
        """
        self.serial_number = serial_number
        self.hotplug_callback = hotplug_callback
        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by(subsystem='usb')
        
    # This has an additional argument of 'Local' that when True, causes 
    # this to search only for the local baochip, and not the DUT.
    def find_acm_device(self, timeout: float = 10.0, local=False) -> Optional[str]:
        """
        Find the latest ttyACM device matching our USB vendor/product ID
        
        Returns:
            Path to ttyACM device (e.g., '/dev/ttyACM0') or None
        """
        logger.info("Searching for ACM device...")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            acm_devices = []
            
            for device in self.context.list_devices(subsystem='tty'):
                # Check if this is an ACM device
                if 'ttyACM' not in device.device_node:
                    continue
                
                # Walk up the device tree to find USB parent
                usb_device = device.find_parent('usb', 'usb_device')
                if usb_device == None:
                    continue
                
                # Check vendor and product ID
                self.vendor = vendor = usb_device.properties.get('ID_VENDOR_ID', '').lower()
                self.product = product = usb_device.properties.get('ID_MODEL_ID', '').lower()
                serial = usb_device.properties.get('ID_SERIAL_SHORT', '')
                
                if local:
                    if (vendor == self.VENDOR_ID) \
                        and ((product == self.PRODUCT_ID_LOCAL) or (product == self.PRODUCT_ID_LOCAL_XOUS)):
                        if self.serial_number and serial != self.serial_number:
                            continue
                        
                        acm_devices.append({
                            'path': device.device_node,
                            'serial': serial,
                            'device': device
                        })
                else:
                    if (vendor == self.VENDOR_ID) \
                        and ((product == self.PRODUCT_ID) or (product == self.PRODUCT_ID_XOUS)):
                        if self.serial_number and serial != self.serial_number:
                            continue
                        
                        acm_devices.append({
                            'path': device.device_node,
                            'serial': serial,
                            'device': device
                        })
            
            if acm_devices:
                # Return the most recently created device
                latest = max(acm_devices, key=lambda d: d['device'].sys_number)
                logger.info(f"Found ACM device: {latest['path']} (Serial: {latest['serial']})")
                return latest['path']
            
            time.sleep(0.05)
        
        logger.error(f"ACM device not found within {timeout}s")
        return None
    
    def find_storage_device(self, timeout: float = 10.0, local=False) -> Optional[Tuple[str, str]]:
        """
        Find the latest block device matching our USB vendor/product ID
        
        Returns:
            Tuple of (device_path, partition_path) e.g., ('/dev/sda', '/dev/sda') or None
        """
        logger.info("Searching for storage device...")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            block_devices = []
            
            for device in self.context.list_devices(subsystem='block', DEVTYPE='disk'):
                # Walk up to find USB parent
                usb_device = device.find_parent('usb', 'usb_device')
                if usb_device == None:
                    continue
                
                vendor = usb_device.properties.get('ID_VENDOR_ID', '').lower()
                product = usb_device.properties.get('ID_MODEL_ID', '').lower()
                serial = usb_device.properties.get('ID_SERIAL_SHORT', '')
                
                if local:
                    if (vendor == self.VENDOR_ID) \
                        and ((product == self.PRODUCT_ID_LOCAL) or (product == self.PRODUCT_ID_LOCAL_XOUS)):
                        if self.serial_number and serial != self.serial_number:
                            continue
                        
                        device_node = device.device_node + '1' # append partition number - this may be brittle
                        block_devices.append({
                            'path': device_node,
                            'serial': serial,
                            'sys_name': device.sys_name
                        })
                else:
                    if (vendor == self.VENDOR_ID) \
                        and ((product == self.PRODUCT_ID) or (product == self.PRODUCT_ID_XOUS)):
                        if self.serial_number and serial != self.serial_number:
                            continue
                        
                        device_node = device.device_node + '1' # append partition number - this may be brittle
                        block_devices.append({
                            'path': device_node,
                            'serial': serial,
                            'sys_name': device.sys_name
                        })            
            if block_devices:
                # Return the most recently created device
                latest = max(block_devices, key=lambda d: d['sys_name'])
                logger.info(f"Found storage device: {latest['path']} (Serial: {latest['serial']})")
                return latest['path'], latest['path']
            
            time.sleep(0.05)
        
        logger.error(f"Storage device not found within {timeout}s")
        return None
    
    def send_command(self, acm_path: str, command: str, timeout: float = 1.0, expect_response = True, prepend_newline = True) -> str:
        """
        Send a command to the ACM interface and return the response
        
        Args:
            acm_path: Path to ACM device
            command: Command string to send
            timeout: Read timeout in seconds
            
        Returns:
            Response string from device
        """
        logger.info(f"Sending command to {acm_path}: {command}")
        
        try:
            with serial.Serial(acm_path, 1_000_000, timeout=timeout) as ser:
                # Clear any pending data
                ser.reset_input_buffer()
                
                # Send command character-by-character; add an inital CR to clear buffer of stale characters
                if prepend_newline:
                    full_command = f"\r{command}\r"
                else:
                    full_command = f"{command}\r"
                for char in full_command:
                    ser.write(char.encode('utf-8'))
                    ser.flush()
                    time.sleep(0.25)  # pause for keyboard relay

                # extra CR causes the "command not recognized" to appear
                if expect_response:
                    ser.write("\r".encode('utf-8'))
                    ser.flush()
                
                    # Read response
                    response = []
                    start_time = time.time()
                    
                    while time.time() - start_time < timeout:
                        chunk = ser.read(4096)

                        if chunk:
                            text = chunk.decode("utf-8", errors="ignore")
                            response.append(text)
                            if "Command not recognized" in text:
                                break
                    
                    result = "".join(response)
                    logger.debug(f"Response: {result.strip()}")
                    return result.strip()
                else:
                    return ""
                
        except serial.SerialException as e:
            logger.error(f"Serial communication error: {e}")
            return ""
    
    def get_volume_label(self, device_path: str) -> Optional[str]:
        """
        Get the volume label of a storage device
        
        Args:
            device_path: Path to block device (e.g., '/dev/sda')
            
        Returns:
            Volume label string or None
        """
        try:
            # Use blkid to get filesystem info
            result = subprocess.run(
                ['sudo', 'blkid', '-s', 'LABEL', '-o', 'value', device_path],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                label = result.stdout.strip()
                logger.info(f"Volume label for {device_path}: {label}")
                return label
            else:
                logger.warning(f"Could not get volume label for {device_path}")
                logger.warning(f"Subprocess output: {result}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.error("Timeout getting volume label")
            return None
        except Exception as e:
            logger.error(f"Error getting volume label: {e}")
            return None
    
    def mount_device(self, device_path: str, mount_point: Optional[Path] = None) -> Optional[Path]:
        """
        Mount a storage device
        
        Args:
            device_path: Path to block device
            mount_point: Optional specific mount point, otherwise creates temp dir
            
        Returns:
            Path to mount point or None on failure
        """
        if mount_point is None:
            mount_point = Path(f"/tmp/baochip_mount_{int(time.time())}")
            mount_point.mkdir(exist_ok=True)
        
        logger.info(f"Mounting {device_path} to {mount_point}")
        
        try:
            result = subprocess.run(
                ['sudo', 'mount', device_path, str(mount_point)],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully mounted to {mount_point}")
                return mount_point
            else:
                logger.error(f"Mount failed: {result.stderr}")
                return None
                
        except Exception as e:
            logger.error(f"Error mounting device: {e}")
            return None
    
    def unmount_device(self, mount_point: Path) -> bool:
        """
        Unmount a storage device and optionally remove mount point
        
        Args:
            mount_point: Path to mount point
            
        Returns:
            True on success, False on failure
        """
        logger.info(f"Unmounting {mount_point}")
        
        try:
            # Sync first to ensure all writes are complete
            subprocess.run(['sync'], timeout=20)
            
            result = subprocess.run(
                ['sudo', 'umount', str(mount_point)],
                capture_output=True,
                text=True,
                timeout=20
            )
            
            if result.returncode == 0:
                logger.info("Successfully unmounted")
                # Clean up temp mount point if it's in /tmp
                if '/tmp/baochip_mount' in str(mount_point):
                    mount_point.rmdir()
                return True
            else:
                logger.error(f"Unmount failed: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"Error unmounting device: {e}")
            return False
    
    def copy_files(self, source_files: List[Path], mount_point: Path, timeout=20) -> bool:
        """
        Copy files to mounted device
        
        Args:
            source_files: List of source file paths
            mount_point: Destination mount point
            
        Returns:
            True if all files copied successfully
        """
        logger.info(f"Copying {len(source_files)} files to {mount_point}")
        
        success = True
        for source in source_files:
            if not source.exists():
                logger.error(f"Source file not found: {source}")
                success = False
                continue
            
            dest = mount_point / source.name
            logger.info(f"Copying {source.name} {md5sum(source)}...")
            
            try:
                # Use subprocess for reliable copy with sudo
                result = subprocess.run(
                    ['sudo', 'cp', str(source), str(dest)],
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                
                if result.returncode == 0:
                    logger.info(f"Successfully copied {source.name}")
                else:
                    logger.error(f"Copy failed for {source.name}: {result.stderr}")
                    success = False
                    
            except Exception as e:
                logger.error(f"Error copying {source.name}: {e}")
                success = False
        
        # Sync is moved to unmount command
        # subprocess.run(['sync'], timeout=timeout)
        
        return success
    
    def wait_for_disconnect(self, acm_path: str, timeout: float = 10.0) -> bool:
        """
        Wait for the ACM device to disconnect
        
        Args:
            acm_path: Path to ACM device to monitor
            timeout: Maximum time to wait
            
        Returns:
            True if device disconnected, False on timeout
        """
        logger.info(f"Waiting for {acm_path} to disconnect...")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if not Path(acm_path).exists():
                logger.info("Device disconnected")
                return True
            time.sleep(0.25)
        
        logger.warning("Timeout waiting for disconnect")
        return False
    
    def wait_for_reconnect(self, wait_acm: bool = True, wait_storage: bool = True, 
                          timeout: float = 30.0, local = False) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
        """
        Wait for device to re-enumerate after a boot command
        
        Args:
            wait_acm: Whether to wait for ACM device
            wait_storage: Whether to wait for storage device
            timeout: Maximum time to wait
            
        Returns:
            Tuple of (acm_path, storage_info) where storage_info is (device, partition)
        """
        logger.info("Waiting for device to re-enumerate...")
        
        acm_path = None
        storage_info = None
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if wait_acm and acm_path is None:
                acm_path = self.find_acm_device(timeout=3, local=local)
            
            if wait_storage and storage_info is None:
                storage_info = self.find_storage_device(timeout=3, local=local)
            
            # Check if we have everything we need
            if (not wait_acm or acm_path) and (not wait_storage or storage_info):
                logger.info("Device re-enumerated successfully")
                return acm_path, storage_info
            
            time.sleep(0.25)
        
        logger.error("Timeout waiting for re-enumeration")
        return acm_path, storage_info

class BaochipCIRunner:
    """Main CI test runner for Baochip device"""
    BOOKEND_START = '_|TT|_'
    BOOKEND_END = '_|TE|_'
    
    def __init__(self, firmware_dir: Path, hotplug_callback=None):
        """
        Initialize CI runner
        
        Args:
            firmware_dir: Directory containing firmware files
            hotplug_callback: Optional hotplug() function
        """
        self.firmware_dir = firmware_dir
        self.device = BaochipDevice(hotplug_callback=hotplug_callback)
        self.results = {}
        self.volume_label = None

    def print_results(self):
        """Print summary of test results"""
        logger.info("=" * 80)
        logger.info("TEST RESULTS SUMMARY")
        logger.info("=" * 80)
        
        for key, value in self.results.items():
            logger.info(f"\n~~~~ {key} ~~~~:")
            logger.info(f"  {value}")

    def load_and_boot(self, files: List, set_bootwait=None, expected_label='BAOCHIP', timeout=20):
        self.volume_label = None
        # Wait for re-enumeration
        acm_path, storage_info = self.device.wait_for_reconnect(
            wait_acm=True, wait_storage=True, timeout=10
        )
        
        if not storage_info:
            logger.error("Device did not re-enumerate with storage")
            raise CommException
        
        device_path, _ = storage_info
        
        # Check volume label
        volume_label = self.device.get_volume_label(device_path)
        self.volume_label = volume_label
        if volume_label != expected_label:
            logger.error(f"Expected volume label 'BAOCHIP', got '{volume_label}'")
            raise LoadException
        
        # Mount and flash applications
        mount_point = self.device.mount_device(device_path)
        if not mount_point:
            logger.error("Failed to mount device")
            raise LoadException
                
        if not self.device.copy_files(files, mount_point, timeout=timeout):
            self.device.unmount_device(mount_point)
            logger.error("Failed to copy application files")
            raise LoadException
        
        if not self.device.unmount_device(mount_point):
            logger.error("Failed to unmount device")
            raise LoadException
        
        # Send boot command
        if not acm_path:
            acm_path = self.device.find_acm_device(timeout=5)
        
        if not acm_path:
            logger.error("Failed to find ACM device for boot command")
            raise CommException
        
        if set_bootwait is not None:
            if set_bootwait:
                logging.info("Enabling boot wait")
                self.device.send_command(acm_path, "bootwait enable", timeout=1, expect_response=True)
            else:
                logging.info("Disabling boot wait")
                self.device.send_command(acm_path, "bootwait disable", timeout=1, expect_response=True)
        self.device.send_command(acm_path, "boot", timeout=1, expect_response=False)
        self.device.wait_for_disconnect(acm_path, timeout=5)

        return True
