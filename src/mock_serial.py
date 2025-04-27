import logger
class MockSerial:
    def __init__(self, *args, **kwargs):
        self.logger = logging.getLogger('MockSerial')
        self.logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(handler)
        self.logger.debug("Initialized MockSerial")

    def write(self, data):
        self.logger.debug(f"MockSerial write: {data}")

    def flush(self):
        self.logger.debug("MockSerial flush called")

    def close(self):
        self.logger.debug("MockSerial closed")