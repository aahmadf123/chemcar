#!/usr/bin/env python3
"""
Simplified ChemE Car Luminol Detector
- Robust sensor initialization with fallbacks
- Creates CSV files immediately
- Better error handling and diagnostics
- Test mode to verify hardware
"""

import argparse, csv, os, sys, time
from datetime import datetime

# Sensor init flags
HAS_TCS = False
HAS_AS7343 = False

# Try importing sensor libraries
try:
    import board, busio
    import adafruit_tcs34725 as TCS
    print("TCS34725 library loaded")
except Exception as e:
    print(f"Warning: TCS34725 library not available: {e}")

try:
    import qwiic_as7343
    print("AS7343 library loaded")
except Exception as e:
    print(f"Warning: AS7343 library not available: {e}")

# Constants
DEFAULT_SAMPLE_HZ = 5.0
DEFAULT_INTERVAL_SEC = None  # Optional explicit period between AS7343 reads
SMOOTH_WINDOW = 4
HYSTERESIS_COUNT = 4
ALPHA_THRESHOLD = 0.25
BETA_THRESHOLD = 0.003
HOLD_OFF_SEC = 0.5

# TCS34725 settings
TCS_INTEGRATION_MS = 200
TCS_GAIN = 4

# AS7343 channel names
AS_CHANNELS = [
    "F1", "F2", "FZ", "F3", "F4", "F5", "FY", "FXL",
    "F6", "F7", "F8", "NIR", "Clear", "VIS", "F2a", "F3a", "F4a", "FYa"
]

def timestamp():
    return time.time()

def iso_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

class RollingAverage:
    """Simple moving average"""
    def __init__(self, window_size):
        self.window = window_size
        self.values = []
    
    def add(self, value):
        self.values.append(value)
        if len(self.values) > self.window:
            self.values.pop(0)
        return sum(self.values) / len(self.values)
    
    def reset(self):
        self.values = []

class EndpointDetector:
    """Detects endpoint with hysteresis"""
    def __init__(self, required_count):
        self.required = required_count
        self.count = 0
        self.triggered = False
    
    def check(self, condition):
        if self.triggered:
            return True
        
        if condition:
            self.count += 1
        else:
            self.count = 0
        
        if self.count >= self.required:
            self.triggered = True
        
        return self.triggered

def init_tcs34725(address=0x29):
    """Initialize TCS34725 with error handling"""
    global HAS_TCS
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        sensor = TCS.TCS34725(i2c, address=address)
        sensor.gain = TCS_GAIN
        sensor.integration_time = TCS_INTEGRATION_MS
        
        # Turn off LED
        try:
            sensor._device.write_byte_data(0x00 | 0x80, 0x03)
            print("  TCS34725 LED OFF")
        except:
            print("  TCS34725 LED control skipped")
        
        # Test read
        r, g, b, c = sensor.color_raw
        print(f"  TCS34725 test read: R={r}, G={g}, B={b}, C={c}")
        
        HAS_TCS = True
        return sensor
    except Exception as e:
        print(f"  TCS34725 initialization failed: {e}")
        print("  Continuing without TCS34725...")
        return None

def init_as7343(address=0x39):
    """Initialize AS7343 with error handling"""
    global HAS_AS7343
    try:
        sensor = qwiic_as7343.QwiicAS7343(address)
        
        if not sensor.is_connected():
            raise RuntimeError("AS7343 not detected on I2C bus")
        
        if not sensor.begin():
            raise RuntimeError("AS7343 begin() failed")
        
        sensor.power_on()
        
        # Configure integration time - important for timing!
        # ATIME = 100 steps, ASTEP = 999 gives ~100ms integration
        try:
            sensor.set_atime(100)
            sensor.set_astep(999)
        except:
            print("  Warning: Could not set AS7343 timing parameters")
        
        if not sensor.set_auto_smux(sensor.kAutoSmux18Channels):
            raise RuntimeError("Failed to set 18-channel mode")
        
        if not sensor.spectral_measurement_enable():
            raise RuntimeError("Failed to enable measurements")
        
        # Wait for first measurement to be ready
        time.sleep(0.2)
        
        # Test read
        if sensor.read_all_spectral_data():
            test_val = sensor.get_data(0)
            print(f"  AS7343 test read: F1={test_val}")
        
        HAS_AS7343 = True
        return sensor
    except Exception as e:
        print(f"  AS7343 initialization failed: {e}")
        print("  Continuing without AS7343...")
        return None

def read_as7343(sensor):
    """Read AS7343 data safely with proper timing"""
    if not sensor or not HAS_AS7343:
        return None
    
    try:
        # Wait for measurement to be ready (if method available)
        if hasattr(sensor, 'data_ready'):
            max_wait = 50  # 50 attempts = ~500ms max wait
            wait_count = 0
            while not sensor.data_ready() and wait_count < max_wait:
                time.sleep(0.01)  # 10ms delay
                wait_count += 1
            
            if wait_count >= max_wait:
                print("Warning: AS7343 timeout waiting for data")
                return None
        else:
            # If data_ready not available, just wait a fixed time
            time.sleep(0.05)
        
        # Now read the data
        if not sensor.read_all_spectral_data():
            return None
        
        data = {}
        for i, name in enumerate(AS_CHANNELS):
            try:
                data[name] = int(sensor.get_data(i))
            except:
                data[name] = 0
        return data
    except Exception as e:
        print(f"Warning: AS7343 read failed: {e}")
        return None

def read_tcs34725(sensor):
    """Read TCS34725 data safely"""
    if not sensor or not HAS_TCS:
        return None
    
    try:
        r, g, b, c = sensor.color_raw
        
        lux = 0
        temp = 0
        try:
            lux = sensor.lux
            temp = sensor.color_temperature
        except:
            pass
        
        return {
            'R': r, 'G': g, 'B': b, 'C': c,
            'lux': lux, 'temp_K': temp
        }
    except Exception as e:
        print(f"Warning: TCS34725 read failed: {e}")
        return None

def get_baseline(as_sensor, tcs_sensor, sample_rate, duration=2.0, sensor_gap=0.0):
    """Get dark baseline (sensors covered) with optional inter-sensor delay"""
    print(f"\nCOVER BOTH SENSORS NOW")
    print(f"Acquiring baseline for {duration} seconds...")
    time.sleep(2.5)  # Give time to cover
    
    n_samples = max(5, int(duration * sample_rate))
    as_sum = {ch: 0 for ch in AS_CHANNELS}
    tcs_sum = {'R': 0, 'G': 0, 'B': 0, 'C': 0}
    as_count = 0
    tcs_count = 0
    
    for i in range(n_samples):
        # Read AS7343
        as_data = read_as7343(as_sensor)
        if as_data:
            for ch, val in as_data.items():
                as_sum[ch] += val
            as_count += 1

        if HAS_AS7343 and HAS_TCS and sensor_gap > 0:
            time.sleep(sensor_gap)

        # Read TCS34725
        tcs_data = read_tcs34725(tcs_sensor)
        if tcs_data:
            tcs_sum['R'] += tcs_data['R']
            tcs_sum['G'] += tcs_data['G']
            tcs_sum['B'] += tcs_data['B']
            tcs_sum['C'] += tcs_data['C']
            tcs_count += 1
        
        time.sleep(1.0 / sample_rate)
    
    # Calculate averages
    as_baseline = {}
    if as_count > 0:
        as_baseline = {ch: as_sum[ch] / as_count for ch in AS_CHANNELS}
        print(f"  AS7343 baseline: {as_count} samples, F2={as_baseline.get('F2', 0):.1f}")
    
    tcs_baseline = {}
    if tcs_count > 0:
        tcs_baseline = {k: tcs_sum[k] / tcs_count for k in tcs_sum}
        print(f"  TCS34725 baseline: {tcs_count} samples, B={tcs_baseline['B']:.1f}")
    
    return as_baseline, tcs_baseline

def setup_csv_files(output_dir, run_id):
    """Create CSV files with headers"""
    as_path = os.path.join(output_dir, f"run_{run_id}_as7343.csv")
    tcs_path = os.path.join(output_dir, f"run_{run_id}_tcs34725.csv")
    
    # AS7343 CSV
    with open(as_path, 'w', newline='') as f:
        writer = csv.writer(f)
        header = ['timestamp', 'run_id']
        header.extend(AS_CHANNELS)
        header.extend(['S_blue', 'S_rel', 'S_smooth', 'slope', 'endpoint'])
        writer.writerow(header)
    
    # TCS34725 CSV
    with open(tcs_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'timestamp', 'run_id', 'R', 'G', 'B', 'C',
            'lux', 'temp_K', 'S_blue', 'S_rel', 'S_smooth', 'slope', 'endpoint'
        ])
    
    print(f"\nCSV files created:")
    print(f"  {as_path}")
    print(f"  {tcs_path}")
    
    return as_path, tcs_path

def main():
    parser = argparse.ArgumentParser(description='ChemE Car Luminol Detector')
    parser.add_argument('--out', default=os.path.expanduser('~/chemcar_data'),
                       help='Output directory')
    parser.add_argument('--rate', type=float, default=DEFAULT_SAMPLE_HZ,
                       help='Sample rate in Hz (ignored when --interval is set)')
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL_SEC,
                       help='Explicit interval in seconds between AS7343 reads; overrides --rate')
    parser.add_argument('--tcs-offset', type=float, default=1.0,
                       help='Time after AS7343 read to trigger TCS34725 read (seconds)')
    parser.add_argument('--sensor-gap', type=float, default=0.02,
                       help='Minimum delay between sequential sensor reads (seconds)')
    parser.add_argument('--test', action='store_true',
                       help='Test mode: just read sensors')
    parser.add_argument('--skip-baseline', action='store_true',
                       help='Skip baseline calibration')
    parser.add_argument('--tcs-addr', type=lambda x: int(x, 0), default=0x29,
                       help='TCS34725 I2C address (default: 0x29)')
    parser.add_argument('--as-addr', type=lambda x: int(x, 0), default=0x39,
                       help='AS7343 I2C address (default: 0x39)')
    args = parser.parse_args()

    period = args.interval if args.interval is not None else 1.0 / args.rate
    if period <= 0:
        raise ValueError("Interval must be positive")

    # Offset when both sensors are present (e.g., AS7343 at t=0s, TCS at t=offset)
    tcs_offset = max(args.sensor_gap, min(args.tcs_offset, period))
    
    print("=" * 70)
    print("CHEME CAR LUMINOL DETECTOR - SIMPLIFIED")
    print("=" * 70)
    
    # Create output directory
    os.makedirs(args.out, exist_ok=True)
    
    # Initialize sensors
    print("\nInitializing sensors...")
    print(f"  Looking for TCS34725 at address 0x{args.tcs_addr:02x}")
    print(f"  Looking for AS7343 at address 0x{args.as_addr:02x}")
    tcs_sensor = init_tcs34725(args.tcs_addr)
    as_sensor = init_as7343(args.as_addr)
    
    if not HAS_TCS and not HAS_AS7343:
        print("\nERROR: No sensors detected!")
        print("\nTroubleshooting:")
        print("1. Check I2C connections")
        print("2. Run: sudo i2cdetect -y 1")
        print("3. Verify sensor power")
        print("4. Check library installations")
        return 1
    
    print(f"\nSensors active: TCS34725={HAS_TCS}, AS7343={HAS_AS7343}")
    
    # Test mode
    if args.test:
        print("\nTEST MODE - Reading sensors 10 times...")
        next_tick = time.monotonic()
        for i in range(10):
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)

            print(f"\n--- Sample {i+1} ---")
            if HAS_AS7343:
                data = read_as7343(as_sensor)
                if data:
                    print(f"AS7343: F2={data['F2']}, NIR={data['NIR']}, Clear={data['Clear']}")
            if HAS_AS7343 and HAS_TCS and tcs_offset > 0:
                time.sleep(tcs_offset)
            if HAS_TCS:
                data = read_tcs34725(tcs_sensor)
                if data:
                    print(f"TCS34725: R={data['R']}, G={data['G']}, B={data['B']}, C={data['C']}")

            next_tick += period
        return 0

    # Setup run
    run_id = iso_timestamp()
    as_csv, tcs_csv = setup_csv_files(args.out, run_id)
    
    # Get baseline
    as_baseline = {}
    tcs_baseline = {}
    if not args.skip_baseline:
        baseline_rate = 1.0 / period
        as_baseline, tcs_baseline = get_baseline(
            as_sensor, tcs_sensor, baseline_rate, sensor_gap=tcs_offset
        )
    else:
        print("\nSkipping baseline calibration")
    
    # Initialize processing
    as_smoother = RollingAverage(SMOOTH_WINDOW)
    tcs_smoother = RollingAverage(SMOOTH_WINDOW)
    as_endpoint = EndpointDetector(HYSTERESIS_COUNT)
    tcs_endpoint = EndpointDetector(HYSTERESIS_COUNT)

    as_last_smooth = None
    tcs_last_smooth = None
    as_s0 = None
    tcs_s0 = None
    anchor_samples = []

    sample_count = 0
    status_counter = 0

    as_s_smooth = 0
    as_slope = 0
    as_end = 0
    tcs_s_smooth = 0
    tcs_slope = 0
    tcs_end = 0

    # Schedule the sensor reads so TCS is offset from AS when both are present
    loop_start = time.monotonic()
    as_next = loop_start
    tcs_next = loop_start if not HAS_AS7343 else loop_start + tcs_offset

    print("\nSTARTING DATA ACQUISITION")
    print("Mix luminol and oxidizer now!")
    print("Press Ctrl+C to stop\n")

    try:
        while True:
            now = time.monotonic()

            # Read AS7343 when its slot comes due
            if HAS_AS7343 and now >= as_next:
                t = timestamp()
                as_data = read_as7343(as_sensor)

                if as_data:
                    # Dark subtract
                    blue_raw = as_data.get('F2', 0)
                    blue_corrected = max(0, blue_raw - as_baseline.get('F2', 0))

                    # Calculate sum of visible channels
                    vis_sum = 0
                    for ch in ['F1', 'F2', 'FZ', 'F3', 'F4', 'F5', 'FY', 'F6', 'F7', 'F8']:
                        vis_sum += max(0, as_data.get(ch, 0) - as_baseline.get(ch, 0))

                    # Blue signal ratio
                    s_blue = blue_corrected / vis_sum if vis_sum > 0 else 0

                    # Establish S0 from first few samples
                    if len(anchor_samples) < 5:
                        anchor_samples.append(s_blue)
                        as_s0 = sum(anchor_samples) / len(anchor_samples)

                    # Relative signal
                    s_rel = s_blue / as_s0 if as_s0 > 0 else 0

                    # Smooth and slope
                    as_s_smooth = as_smoother.add(s_rel)
                    if as_last_smooth is not None:
                        as_slope = (as_s_smooth - as_last_smooth) / period
                    as_last_smooth = as_s_smooth

                    # Endpoint detection
                    condition = (as_s_smooth <= ALPHA_THRESHOLD and
                               abs(as_slope) <= BETA_THRESHOLD)
                    as_end = 1 if as_endpoint.check(condition) else 0

                    # Write to CSV
                    with open(as_csv, 'a', newline='') as f:
                        writer = csv.writer(f)
                        row = [t, run_id]
                        row.extend([as_data.get(ch, 0) for ch in AS_CHANNELS])
                        row.extend([s_blue, s_rel, as_s_smooth, as_slope, as_end])
                        writer.writerow(row)

                    sample_count += 1
                    status_counter += 1

                as_next += period
                if HAS_AS7343 and HAS_TCS:
                    tcs_next = max(tcs_next, as_next - period + tcs_offset)

            # Read TCS34725 when its slot comes due
            if HAS_TCS and now >= tcs_next:
                t = timestamp()
                tcs_data = read_tcs34725(tcs_sensor)

                if tcs_data:
                    # Dark subtract
                    b_corrected = max(0, tcs_data['B'] - tcs_baseline.get('B', 0))
                    r_corrected = max(0, tcs_data['R'] - tcs_baseline.get('R', 0))
                    g_corrected = max(0, tcs_data['G'] - tcs_baseline.get('G', 0))

                    # Blue ratio
                    rgb_sum = r_corrected + g_corrected + b_corrected
                    s_blue = b_corrected / rgb_sum if rgb_sum > 0 else 0

                    # Establish S0
                    if tcs_s0 is None and len(anchor_samples) >= 5:
                        tcs_s0 = s_blue

                    # Relative signal
                    s_rel = s_blue / tcs_s0 if tcs_s0 else 0

                    # Smooth and slope
                    tcs_s_smooth = tcs_smoother.add(s_rel)
                    if tcs_last_smooth is not None:
                        tcs_slope = (tcs_s_smooth - tcs_last_smooth) / period
                    tcs_last_smooth = tcs_s_smooth

                    # Endpoint detection
                    condition = (tcs_s_smooth <= ALPHA_THRESHOLD and
                               abs(tcs_slope) <= BETA_THRESHOLD)
                    tcs_end = 1 if tcs_endpoint.check(condition) else 0

                    # Write to CSV
                    with open(tcs_csv, 'a', newline='') as f:
                        writer = csv.writer(f)
                        row = [t, run_id,
                              tcs_data['R'], tcs_data['G'], tcs_data['B'], tcs_data['C'],
                              tcs_data['lux'], tcs_data['temp_K'],
                              s_blue, s_rel, tcs_s_smooth, tcs_slope, tcs_end]
                        writer.writerow(row)

                    if not HAS_AS7343:
                        sample_count += 1
                        status_counter += 1

                tcs_next += period

            # Print status every 10 sensor events
            if status_counter > 0 and status_counter % 10 == 0:
                status = "t={:.2f}s".format(time.time())
                if HAS_AS7343:
                    status += f" | AS7343: S={as_s_smooth:.4f}, slope={as_slope:+.5f}"
                if HAS_TCS:
                    status += f" | TCS: S={tcs_s_smooth:.4f}, slope={tcs_slope:+.5f}"
                print(status)

            # Check for endpoint
            if (not HAS_AS7343 or as_end) and (not HAS_TCS or tcs_end):
                print(f"\nENDPOINT DETECTED at t={time.time():.2f}s")
                time.sleep(HOLD_OFF_SEC)
                break

            next_wake = min(
                as_next if HAS_AS7343 else float('inf'),
                tcs_next if HAS_TCS else float('inf')
            )
            sleep_duration = next_wake - time.monotonic()
            if sleep_duration > 0:
                time.sleep(sleep_duration)

    except KeyboardInterrupt:
        print("\n\nStopped by user")
    
    print(f"\nData saved:")
    print(f"  AS7343: {as_csv}")
    print(f"  TCS34725: {tcs_csv}")
    print(f"Total samples: {sample_count}")
    print("=" * 70)
    
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
