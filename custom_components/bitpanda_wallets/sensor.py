from datetime import timedelta, datetime
import logging
import asyncio

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import DOMAIN, CONF_API_KEY, CONF_WALLET, CONF_CURRENCY, WALLET_TYPES, BITPANDA_API_URL, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Richte die Bitpanda Wallet Sensoren ein."""
    api_key = entry.data[CONF_API_KEY]
    currency = entry.data[CONF_CURRENCY]
    selected_wallets = entry.options.get(CONF_WALLET, list(WALLET_TYPES.keys()))
    update_interval = float(UPDATE_INTERVAL)
    
    coordinator = BitpandaDataUpdateCoordinator(hass, api_key, currency, update_interval, selected_wallets)
    await coordinator.async_config_entry_first_refresh()

    if not coordinator.data:
        raise ConfigEntryNotReady("No data received from Bitpanda API")

    entities = []
    for wallet_type in selected_wallets:
        if wallet_type in coordinator.data:
            entities.append(BitpandaWalletSensor(coordinator, wallet_type, currency))
        else:
            _LOGGER.warning("Wallet %s not found in Bitpanda API data", wallet_type)

    async_add_entities(entities)

    # Registriere den Update-Listener für Optionen-Änderungen
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle updated options."""
    await hass.config_entries.async_reload(entry.entry_id)

class BitpandaDataUpdateCoordinator(DataUpdateCoordinator):
    """Data update coordinator for Bitpanda API."""
 
    def __init__(self, hass: HomeAssistant, api_key: str, currency: str, update_interval_minutes: float, selected_wallets) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=update_interval_minutes)
        )
        self.api_key = api_key
        self.currency = currency
        self.selected_wallets = selected_wallets
        self.session = async_get_clientsession(hass)
        self.ticker_data = {}
        self.next_update = dt_util.utcnow() + self.update_interval

    async def _async_update_data(self):
        """Aktualisiere Daten über die API."""
        headers = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json"
        }
        data = {}
        try:
            # Ticker-Daten abrufen
            ticker_url = "https://api.bitpanda.com/v1/ticker"
            async with self.session.get(ticker_url) as ticker_response:
                _LOGGER.debug("Antwortstatus für Ticker: %s", ticker_response.status)
                #response_text = await ticker_response.text()
                #_LOGGER.debug("Antworttext für Ticker: %s", response_text)
                ticker_response.raise_for_status()
                ticker_data = await ticker_response.json()
                _LOGGER.debug("Ticker-Daten abgerufen.")
            self.ticker_data = ticker_data

           # Einmalige Abfrage für alle Asset-Wallets
            asset_types = {"STOCK", "INDEX", "METAL", "CRYPTOCOIN", "LEVERAGE", "ETF", "ETC"}
            selected_asset_types = set(self.selected_wallets) & asset_types
            
            if selected_asset_types:
                url = f"{BITPANDA_API_URL}/asset-wallets"
                async with self.session.get(url, headers=headers) as response:
                    _LOGGER.debug("Antwortstatus für Asset-Wallets: %s", response.status)
                    response_text = await response.text()
                    _LOGGER.debug("Antworttext für Asset-Wallets: %s", response_text)
                    response.raise_for_status()
                    asset_data = await response.json()
                    
                    # Verarbeite die Asset-Daten für jeden ausgewählten Wallet-Typ
                    for wallet_type in selected_asset_types:
                        total_balance, wallets_info = self._parse_asset_type(asset_data, wallet_type)
                        data[wallet_type] = {
                            "total_balance": total_balance,
                            "wallets": wallets_info
                        }

            # Separate Abfrage für FIAT (wenn ausgewählt)
            if "FIAT" in self.selected_wallets:
                fiat_url = f"{BITPANDA_API_URL}/fiatwallets"
                async with self.session.get(fiat_url, headers=headers) as response:
                    _LOGGER.debug("Antwortstatus für FIAT: %s", response.status)
                    response_text = await response.text()
                    _LOGGER.debug("Antworttext für FIAT: %s", response_text)
                    response.raise_for_status()
                    fiat_data = await response.json()
                    fiat_balance = self._parse_fiat_wallet(fiat_data)
                    data["FIAT"] = {"total_balance": fiat_balance, "wallets": []}

            # Füge das Aktualisierungsdatum hinzu
            data["last_updated"] = dt_util.utcnow()
            return data

        except Exception as err:
            _LOGGER.error("Fehler beim Abrufen der Daten: %s", err)
            raise UpdateFailed(f"Fehler beim Abrufen der Daten: {err}") from err
        finally:
            # Aktualisiere next_update unabhängig vom Erfolg
            self.next_update = dt_util.utcnow() + self.update_interval

    def _parse_fiat_wallet(self, response_json):
        """Analysiere Fiat-Wallet-Daten und gebe die Balance zurück."""
        wallets = response_json.get('data', [])
        for wallet in wallets:
            attributes = wallet.get('attributes', {})
            currency = attributes.get('fiat_symbol', '')  # Korrekte Schlüsselverwendung
            if currency == self.currency:
                balance = float(attributes.get('balance', 0.0))
                return balance  # Da wir nur ein Fiat Wallet haben, können wir direkt zurückkehren
        return 0.0  # Falls kein Wallet gefunden wurde oder Balance 0 ist

    def _parse_asset_type(self, response_json, wallet_type):
        """Parse specific asset type from the complete asset wallet response."""
        total_balance = 0.0
        wallets_info = []
        
        data = response_json.get('data', {})
        attributes = data.get('attributes', {})
        
        if wallet_type in ['CRYPTOCOIN', 'LEVERAGE']:
            crypto_data = attributes.get('cryptocoin', {}).get('attributes', {}).get('wallets', [])
            
            for wallet in crypto_data:
                wallet_attrs = wallet.get('attributes', {})
                balance_token = float(wallet_attrs.get('balance', 0.0))
                if balance_token > 0:
                    currency = wallet_attrs.get('cryptocoin_symbol', '')
                    is_leverage = currency.endswith('2L') or currency.endswith('1S')
                    
                    if (wallet_type == 'LEVERAGE' and is_leverage) or (wallet_type == 'CRYPTOCOIN' and not is_leverage):
                        price = float(self.ticker_data.get(currency, {}).get(self.currency, 0))
                        balance_converted = balance_token * price
                        total_balance += balance_converted
                        name = wallet_attrs.get('name', '')
                        wallets_info.append({
                            "name": name,
                            "balance_token": balance_token,
                            f"balance_{self.currency.lower()}": round(balance_converted, 2),
                            "currency": currency
                        })
        else:
            # Handle other asset types
            wallet_type_lower = wallet_type.lower()
            if wallet_type_lower in attributes:
                wallet_data = attributes.get(wallet_type_lower, {}).get('attributes', {})
            elif wallet_type_lower in attributes.get('security', {}):
                wallet_data = attributes['security'][wallet_type_lower].get('attributes', {})
            elif wallet_type_lower in attributes.get('commodity', {}):
                wallet_data = attributes['commodity'][wallet_type_lower].get('attributes', {})
            elif wallet_type_lower in attributes.get('index', {}):
                wallet_data = attributes['index'][wallet_type_lower].get('attributes', {})
            else:
                wallet_data = {}

            wallets = wallet_data.get('wallets', [])
            for wallet in wallets:
                wallet_attrs = wallet.get('attributes', {})
                balance_token = float(wallet_attrs.get('balance', 0.0))
                if balance_token > 0:
                    currency = wallet_attrs.get('cryptocoin_symbol', '')
                    price = float(self.ticker_data.get(currency, {}).get(self.currency, 0))
                    balance_converted = balance_token * price
                    total_balance += balance_converted
                    name = wallet_attrs.get('name', '')
                    wallets_info.append({
                        "name": name,
                        "balance_token": balance_token,
                        f"balance_{self.currency.lower()}": round(balance_converted, 2),
                        "currency": currency
                    })

        return total_balance, wallets_info

class BitpandaWalletSensor(CoordinatorEntity, SensorEntity):
    """Repräsentation eines Bitpanda Wallet Sensors."""
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_should_poll = False

    def __init__(self, coordinator: BitpandaDataUpdateCoordinator, wallet_type: str, currency: str) -> None:
        super().__init__(coordinator)
        self.wallet_type = wallet_type
        self.currency = currency
        
        self._attr_name = f"Bitpanda Wallets {wallet_type} {currency}"
        self._attr_unique_id = f"{DOMAIN}_{wallet_type.lower()}_{currency.lower()}"

    @property
    def native_value(self):
        """Gibt die Gesamtbalance des Sensors zurück."""
        wallet_data = self.coordinator.data.get(self.wallet_type, {})
        return round(wallet_data.get('total_balance', 0.0), 2)

    @property
    def native_unit_of_measurement(self):
        """Gibt die Maßeinheit zurück."""
        return self.currency

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        attributes = {
            "last_update": dt_util.as_local(self.coordinator.data.get("last_updated")).isoformat(),
            "next_update": dt_util.as_local(self.coordinator.next_update).isoformat(),
        }
        
        # Falls der wallet_type in der Liste der unterstützten Typen liegt…
        if self.wallet_type.upper() in ['STOCK', 'INDEX', 'METAL', 'CRYPTOCOIN', 'LEVERAGE', 'ETF', 'ETC']:
            # …hole die Wallets. Wir gehen davon aus, dass die Daten in self.coordinator.data im Key wallet_type liegen.
            wallets = self.coordinator.data.get(self.wallet_type, {}).get('wallets', [])
            # Füge den Key nur hinzu, wenn auch tatsächlich Wallets vorhanden sind.
            if wallets:
                # Sortiere die Wallets nach balance_currency absteigend
                currency_key = f"balance_{self.currency.lower()}"
                sorted_wallets = sorted(wallets, key=lambda x: x[currency_key], reverse=True)
                
                # Formatiere die Wallet-Informationen
                formatted_wallets = {}
                for wallet in sorted_wallets:
                    # Einheitliche Formatierung für alle Wallet-Typen
                    formatted_wallet = (
                        f"Token: {wallet['balance_token']} | "
                        f"{self.currency}: {wallet[currency_key]} | "
                        f"Symbol: {wallet['currency']}"
                    )
                    # Verwende überall den Wallet-Namen als Schlüssel
                    formatted_wallets[wallet['name']] = formatted_wallet
                attributes.update(formatted_wallets)
        return attributes

    async def async_added_to_hass(self) -> None:
        """Register update listener."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
