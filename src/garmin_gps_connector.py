"""
garmin_gps_connector.py
-----------------------
Translate aircraft state streaming from the X-Plane Web API into Garmin
“Aviation-In” ASCII sentences and push them to a serial port (or a mock port).

Quick usage
-----------
$ python src/garmin_gps_connector.py          # live serial
$ python src/garmin_gps_connector.py --test   # mock serial (console output)

Configuration keys read from resources/config.yml
-------------------------------------------------
x-plane:
  connection:
    serial:
      device   : /dev/ttyUSB0
      baud     : 9600
      timeout  : 1
    webapi:
      message_rate  : 10     # emit every N update frames
      pause_timeout : 1.5    # silence → synthetic pause
      # Reconnect back-off doubles from 5 s to a max of 10 s
"""

import io
import logging
import os
import sys
import asyncio
import serial
import yaml

from garmin_gps_webapi import ErrorResult, FlightLoopUpdate, GarminGpsWebAPI, PauseState, Disconnected

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MockSerial(io.RawIOBase):
    """
    Lightweight in-memory replacement for :class:`serial.Serial`.

    Used when running the connector with the ``--test`` flag.
    Ensures :class:`io.TextIOWrapper` sees the stream as writable.
    """
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
    Bridge between X-Plane and a physical Garmin GPS.

    Responsibilities
    ----------------
    1. Connect to the X-Plane Web API via :class:`GarminGpsWebAPI`.
    2. Consume high-rate *dataref_update_values* frames (and synthetic
       pause/resume events) produced by :py:meth:`stream_updates`.
    3. Convert the latest aircraft state to a Garmin “Aviation-In” sentence
       via :py:meth:`build_sentence`.
    4. Send sentences out the configured serial port (or :class:`MockSerial`
       when the connector is launched with `--test`).
    5. Auto-reconnect to the Web-API whenever the socket closes, using an
       back-off that starts at 5 s and caps at 10 s.
    """

    logger.info('Starting GarminGpsConnector')

    def __init__(self):
        self.config = GarminGpsConnector.load_config()
        logging.debug('Configuration loaded: {0}'.format(str(self.config.values())))

        self.device = self.config['x-plane']['connection']['serial']['device']

    @staticmethod
    def load_config():
        """
        Load *resources/config.yml* and return it as a nested dict.

        Returns
        -------
        dict
        """
        config = {}
        with open(r'resources/config.yml') as file:
            # The FullLoader parameter handles the conversion from YAML
            # scalar values to Python the dictionary format
            config = yaml.load(file, Loader=yaml.FullLoader)
        return config

    @staticmethod
    def convert_decdeg_to_ddm(dd):
        """
        Convert decimal degrees to degrees-and-decimal-minutes (DDM).

        Returns
        -------
        tuple(int, float)
            (degrees, minutes) where *minutes* is fractional.
        """
        negative = dd < 0
        dd = abs(dd)
        degrees = int(dd)
        minutes = (dd - degrees) * 60

        if negative:
            degrees = -degrees
        return degrees, minutes

    @staticmethod
    def convert_lat(lat_raw):
        """Format latitude for Aviation-In: (hemi, 2-deg, mm.mm*100)."""
        result = GarminGpsConnector.convert_decdeg_to_ddm(lat_raw)
        hemi = 'N'
        if result[0] < 0:
            hemi = 'S'
        deg = abs(result[0])

        minutes = abs(result[1])

        # Returns ('N', '36', '9872') for  −36.9872°  ->   'S 36 9872'
        return "{0}".format(hemi), "{0:02d}".format(deg), "{0:04d}".format(round(minutes * 100))

    @staticmethod
    def convert_lon(lon_raw):
        """Format longitude for Aviation-In: (hemi, 2-deg, mm.mm*100)."""
        result = GarminGpsConnector.convert_decdeg_to_ddm(lon_raw)
        hemi = 'E'
        if result[0] < 0:
            hemi = 'W'
        deg = abs(result[0])

        minutes = abs(result[1])

        # Returns ('N', '36', '9872') for  −36.9872°  ->   'S 36 9872'
        return "{0}".format(hemi), "{0:02d}".format(deg), "{0:04d}".format(round(minutes * 100))


    # ------------------------------------------------------------------ #
    # Helper: build one Aviation-In sentence                              #
    # ------------------------------------------------------------------ #
    def build_sentence(self, latitude, longitude, elevation_m,
                       mag_psi, magnetic_variation_deg, knots):
        """
        Convert aircraft state into a single Aviation-In sentence.

        Parameters
        ----------
        latitude, longitude : float
            Decimal degrees (negative south/west).
        elevation_m : float
            Elevation in metres above MSL.
        mag_psi : float
            Magnetic heading ψ, degrees (0-360).
        magnetic_variation_deg : float
            East positive, West negative.
        knots : int
            Airspeed in knots (0 while paused).

        Returns
        -------
        str
            Complete sentence including STX/ETX and CR/LF line endings.
        """
        altitude_ft = int(elevation_m * 3.28084)
        lat_h, lat_deg, lat_min = self.convert_lat(latitude)
        lon_h, lon_deg, lon_min = self.convert_lon(longitude)
        compass = int(round(mag_psi))
        mag_var_tenths = int(round(magnetic_variation_deg * 10))
        mag_prefix = "W" if mag_var_tenths >= 0 else "E"

        sentence = (
            "z{alt:05d}\r\n"
            "A{lat_h} {lat_deg} {lat_min}\r\n"
            "B{lon_h} {lon_deg} {lon_min}\r\n"
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
            alt=altitude_ft,
            lat_h=lat_h, lat_deg=lat_deg, lat_min=lat_min,
            lon_h=lon_h, lon_deg=lon_deg, lon_min=lon_min,
            comp=compass, spd=knots,
            mag_pref=mag_prefix, mag=abs(mag_var_tenths)
        )

        # Log the outgoing sentence (control chars replaced for readability)
        logger.debug(
            "SENT-GPS ▶ %s",
            sentence.replace(chr(2), "<STX>").replace(chr(3), "<ETX>").strip()
        )
        return sentence
    
    def run_webapi(self, test_mode=False):
        """
        Main entry-point: connect to Web API, stream events and write serial.

        Parameters
        ----------
        test_mode : bool, optional
            If True, use :class:`MockSerial` instead of a real serial port.
        """
        gps_webapi = GarminGpsWebAPI(self.config)
        message_rate = self.config['x-plane']['connection']['webapi'].get('message_rate', 10)
        # Initialize a dictionary to store the last known good values
        last_known_values = {}

        async def main():
            """
            Keep the serial port open and (re)connect to X-Plane's Web-API
            with exponential back-off (5 s → 10 s max) whenever the websocket drops.
            """
            # ----- Serial initialisation (once) ------------------------ #
            if test_mode:
                ser = MockSerial()
            else:
                if not os.path.exists(self.device):
                    logger.error("Serial device not found: %s", self.device)
                    raise FileNotFoundError(self.device)
                ser = serial.Serial(
                    self.device,
                    self.config['x-plane']['connection']['serial']['baud'],
                    timeout=self.config['x-plane']['connection']['serial']['timeout'],
                )
            sio = io.TextIOWrapper(io.BufferedWriter(ser), write_through=True)
            logger.info("GPS serial connection initialised")

            # ----- Reconnect loop -------------------------------------- #
            pause_timeout = self.config['x-plane']['connection']['webapi'].get('pause_timeout', 1.5)
            backoff = 5     # seconds; doubles up to 10 s max on failure
            message_counter = 0

            while True:
                try:
                    # 1) Establish / re-establish the websocket
                    await gps_webapi.fetch_dataref_ids()
                    await gps_webapi.connect_websocket()
                    logger.info("Web-API connected")
                    backoff = 5         # reset back-off after success

                    # 2) Stream events
                    async for event in gps_webapi.stream_updates(pause_timeout):
                        # --- Flight loop frames ------------------------- #
                        if isinstance(event, FlightLoopUpdate):
                            for key, id_ in gps_webapi.dataref_ids.items():
                                if str(id_) in event.values:
                                    last_known_values[key.split('/')[-1]] = event.values[str(id_)]

                            message_counter += 1
                            if message_counter % message_rate:
                                continue

                            paused = int(last_known_values.get('paused', 0))
                            knots = 0 if paused else int(round(last_known_values.get('airspeed_kts_pilot', 0) or 0))

                            sio.write(self.build_sentence(
                                last_known_values.get('latitude', 0.0),
                                last_known_values.get('longitude', 0.0),
                                last_known_values.get('elevation', 0.0),
                                last_known_values.get('mag_psi', 0.0),
                                last_known_values.get('magnetic_variation', 0.0),
                                knots
                            ))
                            sio.flush()
                            continue

                        # --- Pause / Resume ------------------------------ #
                        if isinstance(event, PauseState):
                            last_known_values['paused'] = 1 if event.paused else 0
                            if event.paused:
                                last_known_values['airspeed_kts_pilot'] = 0.0

                            knots = 0 if event.paused else int(round(last_known_values.get('airspeed_kts_pilot', 0) or 0))
                            sio.write(self.build_sentence(
                                last_known_values.get('latitude', 0.0),
                                last_known_values.get('longitude', 0.0),
                                last_known_values.get('elevation', 0.0),
                                last_known_values.get('mag_psi', 0.0),
                                last_known_values.get('magnetic_variation', 0.0),
                                knots
                            ))
                            sio.flush()
                            continue

                        # --- Web-API error frames ------------------------ #
                        if isinstance(event, ErrorResult):
                            logger.warning("Web-API error %s: %s", event.error_code, event.error_message)

                        # --- Websocket dropped --------------------------- #
                        if isinstance(event, Disconnected):
                            raise ConnectionError(event.reason)

                except (OSError, ConnectionError) as exc:
                    # Could not connect or connection dropped: wait & retry
                    logger.warning("%s - retrying in %s s", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10)
                    continue

        asyncio.run(main())

if __name__ == "__main__":
    # Run connector directly; pass --test to enable MockSerial
    test_mode = "--test" in sys.argv
    connector = GarminGpsConnector()
    connector.run_webapi(test_mode=test_mode)