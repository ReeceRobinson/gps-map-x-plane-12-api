import csv
import io
import logging
import os
import socket

import sys
import time

import click
import serial
import yaml

import xpc


class GarminGpsConnector:
    """
    Garmin GPS Hardware connector for X-Plane and AviationIn format capture.pp
    """
    logger = logging.Logger("GarminGpsConnector", logging.DEBUG)

    file_handler = logging.FileHandler('garmin_gps_connector.log')
    formatter = logging.Formatter('%(asctime)s : %(levelname)s : %(name)s : %(message)s')
    file_handler.setFormatter(formatter)

    console_stream = logging.StreamHandler()
    console_stream.setLevel(logging.ERROR)

    # add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_stream)

    logger.info('Starting GarminGpsConnector')

    def __init__(self):
        self.config = GarminGpsConnector.load_config(self)
        logging.debug('Configuration loaded: {0}'.format(str(self.config.values())))

        device = self.config['x-plane']['connection']['serial']['device']
        device_ok = os.path.exists(device)
        if not device_ok:
            self.logger.error(
                'Invalid device setting for x-plane/connection/serial/device. Value is currently {0}'.format(device))
            sys.exit(-1)

    @staticmethod
    def load_config(self):
        config = {}
        with open(r'resources/config.yml') as file:
            # The FullLoader parameter handles the conversion from YAML
            # scalar values to Python the dictionary format
            config = yaml.load(file, Loader=yaml.FullLoader)
        return config

    @staticmethod
    def parse_message(self, msg):
        parts = msg.split('\r\n')
        result = {'altitude': parts[0][2:]}
        latitude_parts = parts[1].split(' ')
        result['latitude_hemi'] = latitude_parts[0][-1]
        result['latitude_deg'] = latitude_parts[1]
        result['latitude_min'] = latitude_parts[2]

        longitude_parts = parts[2].split(' ')
        result['longitude_hemi'] = longitude_parts[0][-1]
        result['longitude_deg'] = longitude_parts[1]
        result['longitude_min'] = longitude_parts[2]

        compass = parts[3][1:]
        result['compass'] = compass

        geo_code = parts[9][1:]
        result['geo_code'] = geo_code
        return result

    def monitor(self):
        """
        Monitor the serial messages emit-ed by X-Plane when GPS AviationIn is enabled.
        This works with serial cable connected to a Windows PC running X-Plane serial port
        and a second computer (Mac/Linux or PC). On the second computer run this application
        in monitoring mode which connects to the other PC at 9600 Baud,N,8,1.
        When X-Plane is running a flight simulation, GPS message data will be captured and logged.
        :return:
        """
        self.logger.info('Connecting to X-Plane...')
        ser = serial.Serial(
            self.config['x-plane']['connection']['serial']['device'],
            self.config['x-plane']['connection']['serial']['baud'],
            timeout=self.config['x-plane']['connection']['serial']['timeout'])

        sio = io.TextIOWrapper(io.BufferedReader(ser), newline='\r\n', write_through=True, line_buffering=False,
                               errors=None)
        self.logger.info('Connected')
        with open(self.config['x-plane']['output']['filename'], 'w+') as f:
            writer = csv.DictWriter(f, fieldnames=['altitude', 'latitude_hemi', 'latitude_deg', 'latitude_min',
                                                   'longitude_hemi', 'longitude_deg', 'longitude_min', 'compass',
                                                   'geo_code'])
            writer.writeheader()
            msg = ""
            self.logger.info('Monitoring starting...')
            while True:
                try:
                    line = sio.readline()
                    line_bytes: bytes = line.encode('utf-8')
                    # STX = 0x02, ETX = 0x03
                    boundary_detected = '\x03'.encode() in line.encode('utf-8')
                    # Process the split of the message over the message boundary
                    if boundary_detected:
                        etx_idx = line_bytes.index('\x03'.encode())
                        etx_offset = len('\x03'.encode())
                        first_part = line_bytes[:etx_idx + etx_offset]
                        msg = '{0}{1}'.format(msg, first_part.decode('utf-8'))
                        if msg.startswith('\x02'):
                            result = GarminGpsConnector.parse_message(self, msg)
                            writer.writerow(result)
                            self.logger.info(result)
                            self.logger.debug(msg)
                        msg = ""
                        if '\x02'.encode() in line_bytes:
                            stx_idx: int = line_bytes.index('\x02'.encode())
                        stx_offset = len('\x02'.encode())
                        if stx_idx:
                            second_part = line_bytes[stx_idx:]
                            msg = second_part.decode('utf-8')

                    else:
                        msg = '{0}{1}'.format(msg, line_bytes.decode('utf-8'))

                except UnicodeDecodeError as ue:
                    self.logger.debug('Serial read error: {}'.format(ue))
                    continue
                except serial.SerialException as e:
                    self.logger.error('Device error: {}'.format(e))
                    break

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

        return "{0}".format(hemi), "{0:02n}".format(deg), "{0:04n}".format(round(minutes * 100))

    @staticmethod
    def convert_lon(lon_raw):
        result = GarminGpsConnector.decdeg2ddm(lon_raw)
        hemi = 'E'
        if result[0] < 0:
            hemi = 'W'
        deg = abs(result[0])

        minutes = abs(result[1])

        return "{0}".format(hemi), "{0:02n}".format(deg), "{0:04n}".format(round(minutes * 100))

    def run_gps(self) -> None:
        """
        Run the GPS connector.
        The serial cable is connected between the X-Plane simulation machine and the physical Garmin GPS.
        The Network/Loopback interface is used to periodically query the running simulation for position
        and speed data. This data is formatted into a valid AviationIn Formatted message and sent via the
        serial connection to the GPS.
        """
        message_template = 'z{0}\x0D\x0AA{1} {2} {3}\x0D\x0AB{4} {5} {6}\x0D\x0AC{7:03n}\x0D\x0AD{8:03n}\x0D\x0AE00000\x0D\x0AGR0000\x0D\x0AI0000\x0D\x0AKL0000\x0D\x0AQ{9}\x0D\x0AS-----\x0D\x0AT---------\r\nw01@\x0D\x0A'
        try:
            ser = serial.Serial(
                self.config['x-plane']['connection']['serial']['device'],
                self.config['x-plane']['connection']['serial']['baud'],
                timeout=self.config['x-plane']['connection']['serial']['timeout'])

            sio = io.TextIOWrapper(io.BufferedWriter(ser), write_through=True, line_buffering=False, errors=None)
            self.logger.info('GPS serial connection initialised.')
            while True:
                with xpc.XPlaneConnect(
                        xpHost=self.config['x-plane']['connection']['network']['host'],
                        xpPort=self.config['x-plane']['connection']['network']['xpport'],
                        port=self.config['x-plane']['connection']['network']['port'],
                        timeout=self.config['x-plane']['connection']['network']['timeout']) as xplane_client:
                    self.logger.info('X-Plane connecting...')
                    try:
                        # Test the connectivity before entering the run loop.
                        xplane_client.getPOSI()
                    except socket.timeout as e:
                        self.logger.info('Timeout connecting to XPlaneConnect, trying again...')
                        continue
                    self.logger.info('X-Plane connected.')
                    while True:
                        try:
                            posi = xplane_client.getPOSI()

                            altitude = "{0:05n}".format(int(float(posi[2]) * 3.28084))
                            latitude = self.convert_lat(posi[0])
                            longitude = self.convert_lon(posi[1])

                            # The following dataRefs are compatible with x-plane 11.
                            data_refs = ["sim/flightmodel/position/mag_psi",
                                         "sim/flightmodel/position/magnetic_variation",
                                         "sim/cockpit2/gauges/indicators/airspeed_kts_pilot"]
                            values = xplane_client.getDREFs(data_refs)
                            compass = int(round(values[0][0]))
                            mag_var = int(round(float(values[1][0]) * 10))
                            mag_prefix = "W"
                            if mag_var < 0:
                                mag_prefix = "E"
                            knots = int(round(values[2][0]))

                            msg = message_template.format(altitude, *latitude, *longitude, compass, knots,
                                                          "{0}{1:03n}".format(mag_prefix, abs(mag_var)))
                            sio.write(msg)
                            sio.flush()
                            self.logger.debug('\n' + msg)
                            time.sleep(1)
                        except ValueError as ve:
                            self.logger.warning(
                                'Error communicating with X-PlaneConnect. Error: {0}'.format(ve)
                            )
                        except Exception as te:
                            self.logger.warning(
                                "Timeout error while communicating with X-Plane. Possibly X-Plane has quit or "
                                "simulation was stopped while access X-Plane menus? Error: {0}".format(
                                    te))

        except Exception as e:
            message = 'Exception occurred. For details see log file. Error message: {0}'.format(e)
            self.logger.error(message)
            self.logger.error(e.__traceback__)
            sio.close()
            ser.close()


@click.command()
@click.option('--mode',
              type=click.Choice(['GPS', 'monitor', 'webapi'], case_sensitive=False))
def main(mode):
    client = GarminGpsConnector()

    if str(mode).lower() == 'monitor':
        client.monitor()
    elif str(mode).lower() == 'webapi':
        client.run_webapi()
    else:
        client.run_gps()

    logging.info('Exit.')


if __name__ == "__main__":
    main()
