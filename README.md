# MQTT_MCP_Server
Contains list of MQTT topics to listen and extracts part of their json to serve as MCP query tools

Streamable-http MCP Server for showing MQTT topics json messages or parts of them. 

Official Home Assistant MCP Server does not show sensor attributes, and I have some sensors like electricity price that have major part of data in attributes. 

## Environment variables
```
MQTT_HOST  MQTT server's address, default 127.0.0.1
MQTT_PORT  MQTT's port, default 1883
MQTT_USERNAME  MQTT username, default empty
MQTT_PASSWORD  MQTT password, default empty
MCP_LISTENER   Address that MCP listener uses, default 127.0.0.1
MCP_PORT      Port that MCP listener uses, default 8000
```
## Tool definition
```
TopicToolSpec(
		tool_name="RainForecast",
		topic="RainRadar2MQTT/sensor/rainradar_home",
		json_pointer="/Attributes/Forecast/",
```
Listens to topic RainRadar2MQTT/sensor/rainradar_home, get from json data for /Attributes/Forecast/ and serves that as tool RainForecast
