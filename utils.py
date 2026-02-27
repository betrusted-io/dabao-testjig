import hashlib
from datetime import datetime
from pathlib import Path

import serial
import threading
import atexit
import time
import logging

from ci_core import TimeoutException
from ci_core import BaochipCIRunner

logger = logging.getLogger(__name__)

def get_file_md5(filepath):
    md5_hash = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()

def get_file_mtime(filepath):
    mtime = Path(filepath).stat().st_mtime
    return datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')


class SerialLogger:
    def __init__(self, port, logfile, baudrate=1_000_000):
        self.ser = serial.Serial(port, baudrate, timeout=1)
        self.filename = logfile
        self.f = open(logfile, 'w')
        self.lock = threading.Lock()
        self.running = True
        
        atexit.register(self.cleanup)
        
        # Start logging thread
        self.thread = threading.Thread(target=self._read_serial, daemon=True)
        self.thread.start()
    
    def _read_serial(self):
        try:
            while self.running:
                if not self.lock.locked():  # Only read if not sending command
                    line = self.ser.readline().decode('utf-8', errors='ignore')
                    if line:
                        self.f.write(line)
                        self.f.flush()
                else:
                    time.sleep(0.01)  # Small delay when locked
        except:
            pass
    
    def send_command(self, command: str, timeout: float = 1.0, expect_response=True) -> str:
        """Send command using the same serial connection"""
        with self.lock:  # Prevent logger from reading while we send/receive
            try:
                # Clear any pending data
                self.ser.reset_input_buffer()
                
                # Send command character-by-character; add an inital CR to clear buffer of stale characters
                full_command = f"\r{command}\r"
                for char in full_command:
                    self.ser.write(char.encode('utf-8'))
                    self.ser.flush()
                    time.sleep(0.25)  # pause for keyboard relay
                
                if expect_response:
                    self.ser.write("\r".encode('utf-8'))
                    self.ser.flush()
                    
                    # Read response
                    response = []
                    start_time = time.time()
                    
                    while time.time() - start_time < timeout:
                        chunk = self.ser.read(4096)
                        if chunk:
                            text = chunk.decode("utf-8", errors="ignore")
                            response.append(text)
                            if "Command not recognized" in text:
                                break
                    
                    return "".join(response).strip()
                else:
                    return ""
                    
            except serial.SerialException as e:
                print(f"Serial communication error: {e}")
                return ""

    def clear_log(self):
        """Clear the log file contents while keeping it open for future writes"""
        with self.lock:  # Prevent reading/writing while clearing
            self.f.seek(0)  # Go to beginning of file
            self.f.truncate()  # Clear everything from current position onward
            self.f.flush()  # Ensure it's written to disk
        
    def cleanup(self):
        self.running = False
        self.ser.close()
        self.f.close()

    def get_log(self) -> str:
        """Get the current contents of the log file as a string"""
        with self.lock:  # Prevent reading/writing while we read
            # Flush any pending writes
            self.f.flush()
            
            with open(self.filename, 'r') as f:
                contents = f.read()
            
            return contents

# def start_serial_logger(port, logfile):
#     ser = serial.Serial(port, 1_000_000, timeout=1)  # adjust baud rate as needed
#     f = open(logfile, 'w')
#
#     def cleanup():
#         ser.close()
#         f.close()
#   
#     atexit.register(cleanup)
#   
#     def read_serial():
#         try:
#             while True:
#                 line = ser.readline().decode('utf-8', errors='ignore')
#                 if line:
#                     f.write(line)
#                     f.flush()
#         except:
#             pass
#
#     thread = threading.Thread(target=read_serial, daemon=True)
#     thread.start()

def wait_for_serial_output(logfile, target_string, timeout=10, instances=1, use_bookends=True, log=False):
    if use_bookends:
        target_string = BaochipCIRunner.BOOKEND_START + target_string + ',' + BaochipCIRunner.BOOKEND_END
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with open(logfile, 'r') as f:
                content = f.read()
                count = content.count(target_string)
                if count >= instances:
                    if log:
                        logger.info(f"{target_string} found {count} times")
                    return count
        except:
            pass
        time.sleep(0.25)
    
    raise TimeoutException(f"'{target_string}' not found in {timeout}s")