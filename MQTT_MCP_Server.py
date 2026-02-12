import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from collections.abc import AsyncIterator

from asyncio_mqtt import Client as MQTTClient, MqttError

from mcp.server.fastmcp import FastMCP

from starlette.applications import Starlette
from starlette.routing import Mount
import uvicorn


# -----------------------------
# Tool/topic configuration
# -----------------------------
@dataclass(frozen=True)
class TopicToolSpec:
	tool_name: str
	topic: str
	json_pointer: str  # e.g. "/Attributes/Forecast/"


TOOLS: list[TopicToolSpec] = [
	TopicToolSpec(
		tool_name="RainForecast",
		topic="RainRadar2MQTT/sensor/rainradar_home",
		json_pointer="/Attributes/Forecast/",
	),
	TopicToolSpec(
		tool_name="ElectricityPricesForToday",
		topic="Entsoe2MQTT/sensor/ElectricityPrice",
		json_pointer="/raw_today/",
	),
	TopicToolSpec(
		tool_name="ElectricityPricesForTomorrow",
		topic="Entsoe2MQTT/sensor/ElectricityPrice",
		json_pointer="/raw_tomorrow/",
	),
]


# -----------------------------
# JSON Pointer extractor (small RFC6901 subset)
# Supports "/a/b/0/" and ignores trailing slashes
# -----------------------------
def json_pointer_get(doc: Any, pointer: str) -> Any:
	if pointer is None or pointer == "" or pointer == "/":
		return doc

	p = pointer.strip()
	while p.endswith("/") and p != "/":
		p = p[:-1]
	if not p.startswith("/"):
		raise ValueError(f"json_pointer must start with '/': {pointer!r}")

	parts = p.split("/")[1:]
	cur: Any = doc
	for raw in parts:
		token = raw.replace("~1", "/").replace("~0", "~")
		if isinstance(cur, list):
			cur = cur[int(token)]
		elif isinstance(cur, dict):
			cur = cur[token]
		else:
			raise TypeError(f"Cannot descend into {type(cur).__name__} at token {token!r}")
	return cur


# -----------------------------
# MQTT cache
# -----------------------------
class MqttJsonCache:
	def __init__(self) -> None:
		self._latest: Dict[str, Tuple[Any, float]] = {}  # topic -> (payload, ts)
		self._events: Dict[str, asyncio.Event] = {}
		self._lock = asyncio.Lock()

	async def set(self, topic: str, payload: Any) -> None:
		async with self._lock:
			self._latest[topic] = (payload, time.time())
			ev = self._events.get(topic)
			if ev is None:
				ev = asyncio.Event()
				self._events[topic] = ev
			ev.set()

	async def get_latest(self, topic: str) -> Optional[Tuple[Any, float]]:
		async with self._lock:
			return self._latest.get(topic)

	async def wait_for_topic(self, topic: str, timeout_s: float) -> bool:
		async with self._lock:
			if topic in self._latest:
				return True
			ev = self._events.get(topic)
			if ev is None:
				ev = asyncio.Event()
				self._events[topic] = ev

		try:
			await asyncio.wait_for(ev.wait(), timeout=timeout_s)
			return True
		except asyncio.TimeoutError:
			return False


async def mqtt_worker(cache: MqttJsonCache, subscribe_topics: list[str]) -> None:
	host = os.getenv("MQTT_HOST", "127.0.0.1")
	port = int(os.getenv("MQTT_PORT", "1883"))
	username = os.getenv("MQTT_USERNAME") or None
	password = os.getenv("MQTT_PASSWORD") or None

	# Subscribe to all topics in a single call (more reliable)
	subs = [(t, 0) for t in subscribe_topics]

	while True:
		try:
			async with MQTTClient(hostname=host, port=port, username=username, password=password) as client:
				await client.subscribe(subs)

				async with client.unfiltered_messages() as messages:
					async for msg in messages:
						topic = str(msg.topic)
						try:
							payload = json.loads(msg.payload.decode("utf-8"))
							await cache.set(topic, payload)
						except Exception:
							# Ignore malformed JSON
							continue
		except MqttError:
			await asyncio.sleep(2.0)  # reconnect backoff


# -----------------------------
# MCP server definition
# -----------------------------
cache = MqttJsonCache()

# json_response=True => tool results are returned as JSON payloads
# stateless_http=True is usually a good idea for streamable-http deployments
mcp = FastMCP("MQTT Tools", stateless_http=True, json_response=True)


def register_topic_tool(spec: TopicToolSpec) -> None:
	@mcp.tool(name=spec.tool_name)
	async def _tool(
		wait_for_first_message: bool = True,
		wait_timeout_s: float = 5.0,
	) -> dict:
		if wait_for_first_message:
			ok = await cache.wait_for_topic(spec.topic, timeout_s=wait_timeout_s)
			if not ok:
				return {
					"ok": False,
					"tool": spec.tool_name,
					"topic": spec.topic,
					"error": "No message received yet (or timed out waiting).",
				}

		latest = await cache.get_latest(spec.topic)
		if latest is None:
			return {
				"ok": False,
				"tool": spec.tool_name,
				"topic": spec.topic,
				"error": "No message received yet.",
			}

		payload, received_ts = latest

		try:
			subset = json_pointer_get(payload, spec.json_pointer)
		except Exception as e:
			return {
				"ok": False,
				"tool": spec.tool_name,
				"topic": spec.topic,
				"received_ts": received_ts,
				"pointer": spec.json_pointer,
				"error": f"Failed to extract subset: {e}",
			}

		return {
			"ok": True,
			"tool": spec.tool_name,
			"topic": spec.topic,
			"received_ts": received_ts,
			"pointer": spec.json_pointer,
			"data": subset,
		}


for spec in TOOLS:
	register_topic_tool(spec)


# -----------------------------
# ASGI app (starts MQTT immediately)
# -----------------------------
subscribe_topics = sorted({spec.topic for spec in TOOLS})

@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
	# Start MCP session manager and MQTT worker at server startup
	mqtt_task = asyncio.create_task(mqtt_worker(cache, subscribe_topics))
	try:
		async with mcp.session_manager.run():
			yield
	finally:
		mqtt_task.cancel()
		with contextlib.suppress(asyncio.CancelledError):
			await mqtt_task


# Mount MCP at "/" so endpoint is "/mcp" (FastMCP’s default streamable path)
# If your client expects "/mcp", this is the standard layout.
app = Starlette(
	routes=[Mount("/", app=mcp.streamable_http_app())],
	lifespan=lifespan,
)


if __name__ == "__main__":
	uvicorn.run(app, host=os.getenv("MCP_LISTENER", "127.0.0.1"), port=os.getenv("MCP_PORT", "8000"))
