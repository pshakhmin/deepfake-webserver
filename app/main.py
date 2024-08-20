from fastapi import FastAPI
from contextlib import asynccontextmanager
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import UploadFile, File
from starlette.requests import Request
import asyncio
import io
import os
import base64
from aio_pika import Message, connect
from typing import MutableMapping
from PIL import Image
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
)
import uuid
import json
from io import BytesIO


HOST = os.environ["RABBITMQ_HOST"]
USERNAME = os.environ["RABBITMQ_DEFAULT_USER"]
PASSWORD = os.environ["RABBITMQ_DEFAULT_PASS"]


class DeepfakeRpcClient:
    connection: AbstractConnection
    channel: AbstractChannel
    callback_queue: AbstractQueue

    def __init__(self) -> None:
        self.futures: MutableMapping[str, asyncio.Future] = {}

    async def connect(self) -> "DeepfakeRpcClient":
        self.connection = await connect(f"amqp://{USERNAME}:{PASSWORD}@{HOST}/")
        self.channel = await self.connection.channel()
        self.callback_queue = await self.channel.declare_queue(exclusive=True)
        await self.callback_queue.consume(self.on_response, no_ack=True)
        print("RPQ Queue connected")
        return self

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            print(f"Bad message {message!r}")
            return

        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result(message.body)

    async def call(self, n: str):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self.futures[correlation_id] = future
        await self.channel.default_exchange.publish(
            Message(
                str(n).encode(),
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
            ),
            routing_key="rpc_queue",
        )
        return await future


class ImageRequest(BaseModel):
    text: str


class ImageResponse(BaseModel):
    probability: float
    lrp: str


def img_to_base64(image):
    imgio = io.BytesIO()
    image = image.convert("RGB")
    image.save(imgio, "JPEG")
    imgio.seek(0)
    return base64.b64encode(imgio.getvalue()).decode("utf-8")


def video_to_base64(video):
    return base64.b64encode(video).decode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global deepfake_rpc
    deepfake_rpc = await DeepfakeRpcClient().connect()
    yield


app = FastAPI(
    title="Deepfake detection",
    description="API обнаружения deepfake",
    lifespan=lifespan,
)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", tags=["HTML"], response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/video", tags=["HTML"], response_class=HTMLResponse)
async def video(request: Request):
    return templates.TemplateResponse(request=request, name="video.html")


@app.post("/process", tags=["API"])
async def process(file: UploadFile = File()):
    image = Image.open(file.file)
    req = {
        "img": img_to_base64(image),
    }
    output = json.loads(await deepfake_rpc.call(json.dumps(req)))

    lrp_image = Image.open(BytesIO(base64.b64decode(output["lrp"])))
    lrp_image = lrp_image.convert("RGBA")
    image = image.convert("RGBA")
    lrp_image = lrp_image.resize((image.width, image.height), Image.Resampling.LANCZOS)
    lrp_image = Image.blend(image, lrp_image, alpha=0.5)

    output["lrp"] = img_to_base64(lrp_image)
    print(output["lrp"])
    return ImageResponse(**output)


@app.post("/processVideo", tags=["API"])
async def process_video(file: UploadFile = File()):
    req = {
        "vid": video_to_base64(await file.read()),
    }
    output = json.loads(await deepfake_rpc.call(json.dumps(req)))
    print(output)
