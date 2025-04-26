import unittest
from unittest.mock import patch, AsyncMock
import yaml
from GarminGpsWebAPI import GarminGpsWebAPI

class TestGarminGpsWebAPI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Load configuration
        with open('resources/config.yml', 'r') as file:
            self.config = yaml.safe_load(file)
        self.api = GarminGpsWebAPI(self.config)

    @patch('aiohttp.ClientSession.get')
    async def test_fetch_dataref_ids(self, mock_get):
        # Mock response data
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            'data': [
                {'id': 1, 'name': 'sim/flightmodel/position/latitude'},  # <-- corrected name
                {'id': 2, 'name': 'sim/flightmodel/position/longitude'}
            ]
        })
        mock_get.return_value.__aenter__.return_value = mock_response

        await self.api.fetch_dataref_ids()
        self.assertEqual(self.api.dataref_ids['sim/flightmodel/position/latitude'], 1)
        self.assertEqual(self.api.dataref_ids['sim/flightmodel/position/longitude'], 2)