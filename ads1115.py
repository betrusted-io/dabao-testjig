#!/usr/bin/env python3

"""
Simple ADS1115 16-bit ADC Driver for Raspberry Pi
Provides basic single-ended voltage reading on channels 0-3
"""

import smbus2
import time

# This does not include the LED current offset
I_OFFSETS = [
    4.4138,  # calibrated
    4.42004, # calibrated
    4.415,   # est
    4.415,   # est
]

class ADS1115:
    """Driver for ADS1115 16-bit 4-channel ADC"""
    
    # I2C Address
    ADDRESS = 0x48
    
    # Register addresses
    REG_CONVERSION = 0x00
    REG_CONFIG = 0x01
    
    # Configuration register bits
    # MUX (input multiplexer) - bits 14:12
    MUX_AIN0 = 0x4000  # Channel 0 to GND
    MUX_AIN1 = 0x5000  # Channel 1 to GND
    MUX_AIN2 = 0x6000  # Channel 2 to GND
    MUX_AIN3 = 0x7000  # Channel 3 to GND
    
    # PGA (programmable gain amplifier) - bits 11:9
    # Using ±4.096V range to accommodate 0-2.56V full scale
    PGA_4_096V = 0x0200
    
    # Mode - bit 8
    MODE_SINGLE = 0x0100  # Single-shot conversion mode
    
    # Data rate - bits 7:5 (128 SPS is a good default)
    DR_128SPS = 0x0080
    
    # Comparator settings (disabled for basic ADC reading)
    COMP_QUE_DISABLE = 0x0003
    
    # OS (operational status) - bit 15
    OS_START_SINGLE = 0x8000  # Start single conversion
    
    def __init__(self, i2c_bus=1, address=ADDRESS):
        """
        Initialize the ADS1115 driver
        
        Args:
            i2c_bus: I2C bus number (typically 1 on Raspberry Pi)
            address: I2C device address (default 0x48)
        """
        self.bus = smbus2.SMBus(i2c_bus)
        self.address = address
        
        # Channel MUX configurations
        self.channel_config = {
            0: self.MUX_AIN0,
            1: self.MUX_AIN1,
            2: self.MUX_AIN2,
            3: self.MUX_AIN3
        }
    
    def read_adc(self, channel):
        """
        Read a 16-bit value from the specified ADC channel
        
        Args:
            channel: ADC channel number (0-3)
            
        Returns:
            16-bit integer value (0-32767 for positive voltages)
            
        Raises:
            ValueError: If channel is not 0-3
        """
        if channel not in [0, 1, 2, 3]:
            raise ValueError("Channel must be 0, 1, 2, or 3")
        
        # Build configuration word
        config = (
            self.OS_START_SINGLE |      # Start single conversion
            self.channel_config[channel] |  # Select channel
            self.PGA_4_096V |           # ±4.096V range
            self.MODE_SINGLE |          # Single-shot mode
            self.DR_128SPS |            # 128 samples per second
            self.COMP_QUE_DISABLE       # Disable comparator
        )
        
        # Write configuration to start conversion
        config_bytes = [(config >> 8) & 0xFF, config & 0xFF]
        self.bus.write_i2c_block_data(self.address, self.REG_CONFIG, config_bytes)
        
        # Wait for conversion to complete (8ms for 128 SPS)
        time.sleep(0.01)
        
        # Read conversion result
        data = self.bus.read_i2c_block_data(self.address, self.REG_CONVERSION, 2)
        
        # Combine bytes into 16-bit value (big-endian)
        raw_value = (data[0] << 8) | data[1]
        
        # Convert from signed to unsigned if needed
        # ADS1115 returns signed 16-bit value
        if raw_value > 32767:
            raw_value -= 65536
            
        return raw_value
    
    def read_current(self, channel):
        """
        Read current from the specified ADC channel
        
        Args:
            channel: ADC channel number (0-3)
            
        Returns:
            Current in mA
        """
        # average over N samples to reduce noise
        raw = 0
        samples = 1
        for i in range(samples):
            raw += self.read_adc(channel)
        raw = raw / samples

        # Convert to voltage based on PGA setting
        # ±4.096V range means each bit = 4.096 / 32768 = 0.000125V
        # I/V coefficient is 4.98
        current = (raw * 0.000125 / 4.98) * 1000.0 - I_OFFSETS[channel]
        
        return current
    
    def close(self):
        """Close the I2C bus connection"""
        self.bus.close()

"""
# Simple usage example
if __name__ == "__main__":
    # Create ADC instance
    adc = ADS1115()
    
    try:
        # Read all channels
        print("Reading ADS1115 channels:")
        for channel in range(4):
            raw_value = adc.read_adc(channel)
            voltage = adc.read_voltage(channel)
            print(f"Channel {channel}: Raw={raw_value:6d}, Voltage={voltage:.4f}V")
    
    finally:
        adc.close()
"""