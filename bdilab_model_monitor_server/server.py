import json
import logging
import os
from http import HTTPStatus
from typing import Dict, Optional

import requests
import tornado.httpserver
import tornado.ioloop
import tornado.web
from bdilab_model_monitor_server.base import CEModel, ModelResponse
from bdilab_model_monitor_server.protocols.request_handler import RequestHandler
from bdilab_model_monitor_server.protocols.common_http import CommonRequestHandler
from cloudevents.sdk import converters
from cloudevents.sdk import marshaller
from cloudevents.sdk.event import v1
from bdilab_model_monitor_server.protocols import Protocol
from bdilab_model_monitor_server.prometheus_metrics.metrics import BdilabMetrics, validate_metrics
import uuid
import numpy as np
from datetime import datetime, timezone

DEFAULT_HTTP_PORT = 8080
CESERVER_LOGLEVEL = os.environ.get("CESERVER_LOGLEVEL", "INFO").upper()
logging.basicConfig(level=CESERVER_LOGLEVEL)

DEFAULT_LABELS = {
    "deployment_namespace": os.environ.get(
        "DEPLOYMENT_NAMESPACE", "NOT_IMPLEMENTED"
    )
}


class CEServer(object):
    def __init__(
        self,
        protocol: Protocol,
        event_type: str,
        event_source: str,
        http_port: int = DEFAULT_HTTP_PORT,
        reply_url: str = None,
    ):
        """
        CloudEvents server

        Parameters
        ----------
        protocol
             wire protocol
        http_port
             http port to listen on
        """
        self.registered_model: Optional[CEModel] = None
        self.http_port = http_port
        self.protocol = protocol
        self.reply_url = reply_url
        self._http_server: Optional[tornado.httpserver.HTTPServer] = None
        self.event_type = event_type
        self.event_source = event_source
        self.bdilab_metrics = BdilabMetrics(
            extra_default_labels=DEFAULT_LABELS
        )

    def create_application(self):
        return tornado.web.Application(
            [
                (
                    r"/",
                    EventHandler,
                    dict(
                        protocol=self.protocol,
                        model=self.registered_model,
                        reply_url=self.reply_url,
                        event_type=self.event_type,
                        event_source=self.event_source,
                        bdilab_metrics=self.bdilab_metrics
                    ),
                ),
                (r"/protocol", ProtocolHandler, dict(protocol=self.protocol)),
                # Prometheus Metrics API that returns prometheus_metrics for model servers
                (
                    r"/v1/metrics",
                    MetricsHandler,
                    dict(
                        bdilab_metrics=self.bdilab_metrics
                    ),
                ),
            ]
        )

    def start(self, model: CEModel):
        """
        Start the server
        """
        self.register_model(model)

        self._http_server = tornado.httpserver.HTTPServer(
            self.create_application())

        logging.info("Listening on port %s" % self.http_port)
        self._http_server.bind(self.http_port)
        self._http_server.start(1)  # Single worker at present
        tornado.ioloop.IOLoop.current().start()

    def register_model(self, model: CEModel):
        if not model.name:
            raise Exception(
                "Failed to register model, model.name must be provided.")
        self.registered_model = model
        logging.info("Registering model:" + model.name)


def get_request_handler(protocol, request: Dict) -> RequestHandler:
    """
    Create a request handler for the data

    Parameters
    ----------
    protocol
         Protocol to use
    request
         The incoming request
    Returns
    -------
         A Request Handler for the desired protocol

    """
    if protocol == Protocol.common_http:
        return CommonRequestHandler(request)
    else:
        raise Exception(f"Unknown protocol {protocol}")


def sendCloudEvent(event: v1.Event, url: str):
    """
    Send CloudEvent

    Parameters
    ----------
    event
         CloudEvent to send
    url
         Url to send event

    """
    http_marshaller = marshaller.NewDefaultHTTPMarshaller()
    binary_headers, binary_data = http_marshaller.ToRequest(
        event, converters.TypeBinary, json.dumps
    )

    logging.info("binary CloudEvent")
    for k, v in binary_headers.items():
        logging.info("{0}: {1}\r\n".format(k, v))
    logging.info(binary_data)

    response = requests.post(url, headers=binary_headers, data=binary_data)
    response.raise_for_status()


class EventHandler(tornado.web.RequestHandler):
    def initialize(
        self,
        protocol: str,
        model: CEModel,
        reply_url: str,
        event_type: str,
        event_source: str,
        bdilab_metrics: BdilabMetrics
    ):
        self.protocol = protocol
        self.model = model
        self.reply_url = reply_url
        self.event_type = event_type
        self.event_source = event_source
        self.bdilab_metrics = bdilab_metrics

    def post(self):
        """
        Handle post request. Extract data. 
        """
        try:
            body = json.loads(self.request.body)
        except json.decoder.JSONDecodeError as e:
            raise tornado.web.HTTPError(
                status_code=HTTPStatus.BAD_REQUEST,
                reason="Unrecognized request format: %s" % e,
            )

        # Extract payload from request
        request_handler: RequestHandler = get_request_handler(
            self.protocol, body)
        request_handler.validate()
        # y_pred, y_true, task_type, metrics_type = request_handler.extract_request()
        y_pred, y_true, task_type = request_handler.extract_request()

        # Create event from request body
        event = v1.Event()
        http_marshaller = marshaller.NewDefaultHTTPMarshaller()
        event = http_marshaller.FromRequest(
            event, self.request.headers, self.request.body, json.loads
        )
        logging.debug(json.dumps(event.Properties()))

        # Extract any desired request headers
        headers = {}

        for (key, val) in self.request.headers.get_all():
            headers[key] = val
        headers["task_type"] = task_type

        inputs = {}
        try:
            inputs["y_true"] = np.array(y_true)
            inputs["y_pred"] = np.array(y_pred)
        except Exception as e:
            raise Exception(
                "Failed to initialize NumPy array from inputs: %s, %s" % (e, inputs)
            )
        response: Optional[ModelResponse] = self.model.process_event(inputs, headers)

        runtime_metrics = response.metrics
        if runtime_metrics is not None:
            if validate_metrics(runtime_metrics):
                self.bdilab_metrics.update(runtime_metrics, self.event_type)
            else:
                logging.error("Metrics returned are invalid: " + str(runtime_metrics))
        if response.data is not None:
            # Create event from response if reply_url is active
            if not self.reply_url == "":
                if event.EventID() is None or event.EventID() == "":
                    resp_event_id = uuid.uuid1().hex
                else:
                    resp_event_id = event.EventID()
                revent = (
                    v1.Event()
                        .SetContentType("application/json")
                        .SetData(response.data)
                        .SetEventID(resp_event_id)
                        .SetSource(self.event_source)
                        .SetEventType(self.event_type)
                        .SetEventTime(datetime.now(timezone.utc).isoformat())   # 符合RFC 3339的时间戳
                        .SetExtensions(event.Extensions())
                )
                logging.debug(json.dumps(revent.Properties()))
                sendCloudEvent(revent, self.reply_url)
            self.write(json.dumps(response.data))


class LivenessHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("Alive")


class ProtocolHandler(tornado.web.RequestHandler):
    def initialize(self, protocol: Protocol):
        self.protocol = protocol

    def get(self):
        self.write(str(self.protocol.value))

class MetricsHandler(tornado.web.RequestHandler):
    def initialize(self, bdilab_metrics: BdilabMetrics):
        self.bdilab_metrics = bdilab_metrics

    def get(self):
        metrics, mimetype = self.bdilab_metrics.generate_metrics()
        self.set_header("Content-Type", mimetype)
        self.write(metrics)
