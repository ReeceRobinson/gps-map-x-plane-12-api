import io
import logging
import os
import asyncio
import json
import websockets
import math
import click
import serial
import yaml
import time
from garmin_gps_webapi import GarminGpsWebAPI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MockSerial(io.RawIOBase):
    def __init__(self, *args, **kwargs):
        self.logger = logging.getLogger('MockSerial')
        self.logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(handler)
        self.logger.debug("Initialized MockSerial")

    def write(self, data):
        if isinstance(data, (bytes, bytearray, memoryview)):
            try:
                decoded_data = data.tobytes().decode('utf-8', errors='replace')
            except AttributeError:
                decoded_data = data.decode('utf-8', errors='replace')
            self.logger.debug(f"MockSerial write: {decoded_data}")
            return len(data)
        else:
            self.logger.info(f"MockSerial write: {data}")
            return len(data)

    def flush(self):
        self.logger.debug("MockSerial flush called")

    def close(self):
        self.logger.debug("MockSerial closed")

    def writable(self):
        return True

class GarminGpsConnector:
    """
    Garmin GPS Hardware connector for X-Plane and AviationIn format capture.
    """
    file_handler = logging.FileHandler('garmin_gps_connector.log')
    formatter = logging.Formatter('%(asctime)s : %(levelname)s : %(name)s : %(message)s')
    file_handler.setFormatter(formatter)

    # add handlers to logger
    logger.addHandler(file_handler)

    logger.info('Starting GarminGpsConnector')

    def __init__(self):
        self.config = GarminGpsConnector.load_config()
        logging.debug('Configuration loaded: {0}'.format(str(self.config.values())))

        self.device = self.config['x-plane']['connection']['serial']['device']
        device_ok = os.path.exists(self.device)
        if not device_ok:
            logging.error(
                'Invalid device setting for x-plane/connection/serial/device. Value is currently {0}'.format(self.device))
            raise FileNotFoundError(f'Device not found: {self.device}')

    @staticmethod
    def load_config():
        config = {}
        with open(r'resources/config.yml') as file:
            # The FullLoader parameter handles the conversion from YAML
            # scalar values to Python the dictionary format
            config = yaml.load(file, Loader=yaml.FullLoader)
        return config

    @staticmethod
    def decdeg2ddm(dd):
        negative = dd < 0
        dd = abs(dd)
        degrees = int(dd)
        minutes = (dd - degrees) * 60

        if negative:
            if degrees > 0:
                degrees = -degrees
            elif minutes > 0:
                minutes = -minutes
        return degrees, minutes

    @staticmethod
    def convert_lat(lat_raw):
        result = GarminGpsConnector.decdeg2ddm(lat_raw)
        hemi = 'N'
        if result[0] < 0:
            hemi = 'S'
        deg = abs(result[0])

        minutes = abs(result[1])

        return "{0}".format(hemi), "{0:02d}".format(deg), "{0:04d}".format(round(minutes * 100))

    @staticmethod
    def convert_lon(lon_raw):
        result = GarminGpsConnector.decdeg2ddm(lon_raw)
        hemi = 'E'
        if result[0] < 0:
            hemi = 'W'
        deg = abs(result[0])

        minutes = abs(result[1])

        return "{0}".format(hemi), "{0:02d}".format(deg), "{0:04d}".format(round(minutes * 100))

    def run_webapi(self, test_mode=False):
        """
        Run the GPS connector using the Web API.
        """
        gps_webapi = GarminGpsWebAPI(self.config)
        message_rate = self.config['x-plane']['connection']['webapi'].get('message_rate', 10)
        # Initialize a dictionary to store the last known good values
        last_known_values = {}

        async def main():
            await gps_webapi.fetch_dataref_ids()
            await gps_webapi.connect_websocket()

            message_counter = 0
            # Open serial connection
            if test_mode:
                ser = MockSerial()
            else:
                ser = serial.Serial(
                    self.config['x-plane']['connection']['serial']['device'],
                    self.config['x-plane']['connection']['serial']['baud'],
                    timeout=self.config['x-plane']['connection']['serial']['timeout'])
            sio = io.TextIOWrapper(io.BufferedWriter(ser), write_through=True, line_buffering=False, errors=None)
            logger.info('GPS serial connection initialized.')

            try:
                # Timeout (seconds) with no Web‑API traffic before we assume the sim is paused
                pause_timeout = self.config['x-plane']['connection']['webapi'].get('pause_timeout', 1.5)
                last_packet_time = time.time()

                while True:
                    try:
                        message = await asyncio.wait_for(gps_webapi.websocket.recv(), timeout=pause_timeout)
                        last_packet_time = time.time()          # got traffic → refresh timer
                    except asyncio.TimeoutError:
                        logger.debug(
                            f"FREEZE pkt lat={last_known_values.get('latitude')} "
                            f"lon={last_known_values.get('longitude')}"
                        )
                        # No traffic for pause_timeout seconds → assume simulator paused
                        first_silence = last_known_values.get('paused') != 1
                        if first_silence:
                            logger.debug(f"No data for {pause_timeout}s – assuming simulator paused.")

                        # Mark paused and freeze airspeed
                        last_known_values['paused'] = 1
                        last_known_values['airspeed_kts_pilot'] = 0.0

                        # Re‑use existing builder to send a frozen packet *every* timeout
                        # Only send if we already have a valid position
                        if 'latitude' not in last_known_values or 'longitude' not in last_known_values:
                            continue  # wait for first dataref update before freezing
                        latitude  = last_known_values.get('latitude', 0.0)
                        longitude = last_known_values.get('longitude', 0.0)
                        elevation = last_known_values.get('elevation', 0.0)
                        mag_psi   = last_known_values.get('mag_psi', 0.0)
                        magnetic_variation = last_known_values.get('magnetic_variation', 0.0)

                        altitude = f"{int(elevation * 3.28084):05d}"
                        lat_h, lat_deg, lat_min = self.convert_lat(latitude)
                        lon_h, lon_deg, lon_min = self.convert_lon(longitude)
                        compass = int(round(mag_psi))
                        mag_var = int(round(magnetic_variation * 10))
                        mag_prefix = "W" if mag_var >= 0 else "E"

                        frozen_msg = (
                            "z{alt}\r\n"
                            "A{lat_h} {lat_deg} {lat_min}\r\n"
                            "B{lon_h} {lon_deg} {lon_min}\r\n"
                            "C{comp:03d}\r\n"
                            "D000\r\n"
                            "E00000\r\n"
                            "GR0000\r\n"
                            "I0000\r\n"
                            "KL0000\r\n"
                            "Q{mag_pref}{mag:03d}\r\n"
                            "S-----\r\n"
                            "T---------\r\n"
                            "w01@\r\n"
                        ).format(
                            alt=altitude,
                            lat_h=lat_h, lat_deg=lat_deg, lat_min=lat_min,
                            lon_h=lon_h, lon_deg=lon_deg, lon_min=lon_min,
                            comp=compass,
                            mag_pref=mag_prefix,
                            mag=abs(mag_var)
                        )
                        sio.write(frozen_msg)
                        sio.flush()
                        continue  # go back to waiting for traffic

                    data = json.loads(message)
                    logger.debug(f"RAW message_type={data.get('type')} payload={data}")
                    message_type = data.get("type")
                    if message_type == "dataref_update_values":
                        values = data.get("data", {})

                        # Extract values using dataref IDs
                        data_fields = {
                            'latitude': values.get(str(gps_webapi.dataref_ids["sim/flightmodel/position/latitude"])),
                            'longitude': values.get(str(gps_webapi.dataref_ids["sim/flightmodel/position/longitude"])),
                            'elevation': values.get(str(gps_webapi.dataref_ids["sim/flightmodel/position/elevation"])),
                            'mag_psi': values.get(str(gps_webapi.dataref_ids["sim/flightmodel/position/mag_psi"])),
                            'magnetic_variation': values.get(str(gps_webapi.dataref_ids["sim/flightmodel/position/magnetic_variation"]), 0.0),
                            'airspeed_kts_pilot': values.get(str(gps_webapi.dataref_ids["sim/cockpit2/gauges/indicators/airspeed_kts_pilot"]),0),
                            'paused': values.get(str(gps_webapi.dataref_ids["sim/time/paused"]), 0)
                        }

                        # Update last known values with new data if available
                        for key, value in data_fields.items():
                            if value is not None:
                                last_known_values[key] = value

                        # Rate‑limit outgoing GPS sentences, but always refresh the cache
                        message_counter += 1
                        if message_counter % message_rate != 0:
                            continue  # cache updated, no packet this cycle

                        # Now that last_known_values is up to date, evaluate pause state
                        paused = int(last_known_values.get('paused', 0))
                        if paused:
                            logger.debug("Simulator is paused; Freezing GPS position.")
                        elif "paused" in last_known_values and last_known_values["paused"] == 0:
                            logger.debug("Simulator un-paused; Resuming GPS position.")

                        # Fill missing last known values with safe defaults
                        defaults = {
                            'latitude': 0.0,
                            'longitude': 0.0,
                            'elevation': 0.0,
                            'mag_psi': 0.0,
                            'magnetic_variation': 0.0,
                            'airspeed_kts_pilot': 0.0,
                            'paused': 0
                        }

                        for key in defaults:
                            if key not in last_known_values:
                                logger.warning(f"Missing {key}, applying default {defaults[key]}")
                                last_known_values[key] = defaults[key]

                        # Retrieve the latest data for processing
                        latitude = last_known_values['latitude']
                        longitude = last_known_values['longitude']
                        elevation = last_known_values['elevation']
                        mag_psi = last_known_values['mag_psi']
                        magnetic_variation = last_known_values['magnetic_variation']
                        airspeed_kts_pilot = last_known_values['airspeed_kts_pilot']

                        # Convert and format values as needed
                        altitude = "{0:05n}".format(int(float(elevation) * 3.28084))
                        latitude_formatted = self.convert_lat(latitude)
                        longitude_formatted = self.convert_lon(longitude)
                        compass = int(round(mag_psi))
                        mag_var = int(round(float(magnetic_variation) * 10))
                        # X-Plane:  +E  / –W    |
                        # Garmin Q: W=+ / E=–   |  therefore invert the sign ↓
                        mag_prefix = "W" if mag_var >= 0 else "E"
                        # Set airspeed: 0 while paused, otherwise use the live value
                        if paused:
                            knots = 0
                        else:
                            knots = int(round(airspeed_kts_pilot)) if airspeed_kts_pilot is not None and not math.isnan(airspeed_kts_pilot) else 0

                        # Construct the message
                        message_template = 'z{0}\x0D\x0AA{1} {2} {3}\x0D\x0AB{4} {5} {6}\x0D\x0AC{7:03d}\x0D\x0AD{8:03d}\x0D\x0AE00000\x0D\x0AGR0000\x0D\x0AI0000\x0D\x0AKL0000\x0D\x0AQ{9}\x0D\x0AS-----\x0D\x0AT---------\r\nw01@\x0D\x0A'
                        msg = message_template.format(
                            altitude,
                            *latitude_formatted,
                            *longitude_formatted,
                            compass,
                            knots,
                            "{0}{1:03n}".format(mag_prefix, abs(mag_var))
                        )

                        # Send the message to the GPS device
                        sio.write(msg)
                        sio.flush()
                        logger.debug('\n' + msg)

                    elif message_type == "dataref_values":
                        params = data.get("params", {})
                        datarefs = params.get("datarefs") or params.get("values") or []

                        # If the response is a simple mapping {id: value, ...} put it into list form
                        if not datarefs and "data" in data and isinstance(data["data"], dict):
                            datarefs = [{'id': int(k), 'value': v} for k, v in data["data"].items()]

                        for entry in datarefs:
                            logger.info(f"Dataref ID {entry['id']} = {entry['value']}")
                            if entry['id'] == gps_webapi.dataref_ids["sim/time/paused"]:
                                # --- Robust parse: accept int, bool, or str ("0"/"1"/"true"/"false") ---
                                raw = entry['value']
                                if isinstance(raw, bool):
                                    new_paused = 1 if raw else 0
                                elif isinstance(raw, (int, float)):
                                    new_paused = int(raw)
                                elif isinstance(raw, str):
                                    new_paused = 1 if raw.strip().lower() in ("1", "true", "yes") else 0
                                else:
                                    logger.warning(f"Unexpected paused value type: {type(raw)}; treating as 0")
                                    new_paused = 0

                                prev_paused = int(last_known_values.get('paused', -1))
                                last_known_values['paused'] = new_paused

                                # Only act when the state toggles (0 ↔ 1)
                                if new_paused != prev_paused:
                                    # Freeze or restore airspeed
                                    if new_paused == 1:            # just paused
                                        last_known_values['airspeed_kts_pilot'] = 0.0
                                    # else: keep the last live speed

                                    # Re‑use existing builder to send a single packet
                                    latitude  = last_known_values.get('latitude', 0.0)
                                    longitude = last_known_values.get('longitude', 0.0)
                                    elevation = last_known_values.get('elevation', 0.0)
                                    mag_psi   = last_known_values.get('mag_psi', 0.0)
                                    magnetic_variation = last_known_values.get('magnetic_variation', 0.0)
                                    airspeed_kts_pilot = last_known_values.get('airspeed_kts_pilot', 0.0)

                                    altitude = f"{int(elevation * 3.28084):05d}"
                                    lat_h, lat_deg, lat_min = self.convert_lat(latitude)
                                    lon_h, lon_deg, lon_min = self.convert_lon(longitude)
                                    compass = int(round(mag_psi))
                                    mag_var = int(round(magnetic_variation * 10))
                                    mag_prefix = "W" if mag_var >= 0 else "E"
                                    knots = 0 if new_paused == 1 else int(round(airspeed_kts_pilot))

                                    msg = (
                                        "z{alt}\r\n"
                                        "A{lat_h}{lat_deg} {lat_min}\r\n"
                                        "B{lon_h}{lon_deg} {lon_min}\r\n"
                                        "C{comp:03d}\r\n"
                                        "D{spd:03d}\r\n"
                                        "E00000\r\n"
                                        "GR0000\r\n"
                                        "I0000\r\n"
                                        "KL0000\r\n"
                                        "Q{mag_pref}{mag:03d}\r\n"
                                        "S-----\r\n"
                                        "T---------\r\n"
                                        "w01@\r\n"
                                    ).format(
                                        alt=altitude,
                                        lat_h=lat_h, lat_deg=lat_deg, lat_min=lat_min,
                                        lon_h=lon_h, lon_deg=lon_deg, lon_min=lon_min,
                                        comp=compass,
                                        spd=knots,
                                        mag_pref=mag_prefix,
                                        mag=abs(mag_var)
                                    )
                                    sio.write(msg)
                                    sio.flush()

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket connection closed.")
            finally:
                sio.close()
                ser.close()

        asyncio.run(main())

@click.group()
@click.pass_context
def cli(ctx):
    """Garmin GPS Connector CLI."""
    pass

@cli.command()
@click.option('--test', is_flag=True, help='Enable test mode with mocked serial port.')
def run_webapi(test):
    """Run the GPS connector using the Web API."""
    connector = GarminGpsConnector()
    connector.run_webapi(test_mode=test)

if __name__ == "__main__":
    cli()