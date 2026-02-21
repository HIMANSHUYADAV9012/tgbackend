from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from telegram import Bot
import json
from typing import Dict, List, Optional
from datetime import datetime
import asyncio
import base64
import traceback

app = FastAPI()

# 🔧 Replace with your actual bot token and chat ID
BOT_TOKEN = "8469613543:AAEG5_OxiBEbweHIeBUqts7pXjormS9kwbI"
ADMIN_CHAT_ID = "5029478739"

bot = Bot(token=BOT_TOKEN)

# ====================== CORS ======================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Adjust ports
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ====================== GLOBAL STATE ======================
rooms: Dict[str, List[tuple]] = {}                 # room_id -> [(websocket, username)]
last_seen: Dict[str, Dict[str, Optional[datetime]]] = {}
last_update_id = 0
polling_task = None

# ====================== WEBSOCKET ENDPOINT ======================
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, username: str = Query(...)):
    await websocket.accept()

    if room_id not in rooms:
        rooms[room_id] = []
        last_seen[room_id] = {}

    # Remove old connection for the same user (prevents duplicates)
    rooms[room_id] = [(ws, u) for (ws, u) in rooms[room_id] if u != username]
    rooms[room_id].append((websocket, username))
    last_seen[room_id][username] = None  # online

    await broadcast_status(room_id, username, "online")

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")

            if msg_type == "message":
                await broadcast_to_others(room_id, message, username)
                # Forward text message to Telegram
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=message.get('text', '')
                )

            elif msg_type == "typing":
                await broadcast_to_others(room_id, {"type": "typing"}, username)

            elif msg_type == "reaction":
                await broadcast_to_others(room_id, message, username)

            elif msg_type == "read":
                await broadcast_to_others(room_id, message, username)

            elif msg_type == "image":
                await broadcast_to_others(room_id, message, username)
                # Forward image to Telegram
                try:
                    data_url = message['url']
                    if data_url.startswith('data:image'):
                        header, encoded = data_url.split(',', 1)
                        image_data = base64.b64decode(encoded)
                        await bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=image_data)
                        print("📸 Image forwarded to Telegram")
                except Exception as e:
                    print(f"❌ Failed to send image to Telegram: {e}")

    except WebSocketDisconnect:
        rooms[room_id] = [(ws, u) for (ws, u) in rooms[room_id] if ws != websocket]
        last_seen[room_id][username] = datetime.now()
        await broadcast_status(room_id, username, "offline")

async def broadcast_to_others(room_id: str, message: dict, sender: str):
    """Send message to all clients in the room except the sender."""
    for ws, user in rooms.get(room_id, []):
        if user != sender:
            try:
                await ws.send_text(json.dumps(message))
            except:
                pass

async def broadcast_status(room_id: str, username: str, status: str):
    """Notify others about online/offline status."""
    data = {
        "type": "status",
        "user": username,
        "status": status
    }
    if status == "offline":
        data["last_seen"] = last_seen[room_id][username].isoformat()
    await broadcast_to_others(room_id, data, username)

# ====================== TELEGRAM POLLING (RECEIVE) ======================
async def telegram_polling():
    global last_update_id
    while True:
        try:
            # Delete webhook if any (ensures polling works)
            try:
                await bot.delete_webhook()
            except:
                pass

            updates = await bot.get_updates(offset=last_update_id + 1, timeout=30)
            for update in updates:
                last_update_id = update.update_id
                if update.message and str(update.message.chat_id) == ADMIN_CHAT_ID:
                    sender_name = update.message.from_user.first_name or "Himanshu"
                    msg_id = f"tg-{update.update_id}"

                    if update.message.text:
                        message_data = {
                            "type": "message",
                            "id": msg_id,
                            "text": update.message.text,
                            "sender": sender_name
                        }
                    elif update.message.photo:
                        # Use local proxy URL
                        photo = update.message.photo[-1]
                        file_id = photo.file_id
                        local_url = f"/telegram_image/{file_id}"
                        message_data = {
                            "type": "image",
                            "id": msg_id,
                            "url": local_url,
                            "sender": sender_name
                        }
                    else:
                        continue

                    # Broadcast to all clients in the room (you can make this dynamic)
                    room_id = "shreya-himanshu"
                    for ws, _ in rooms.get(room_id, []):
                        try:
                            await ws.send_text(json.dumps(message_data))
                        except:
                            pass
        except Exception as e:
            print(f"Telegram polling error: {e}")
            if "Conflict" in str(e):
                print("Webhook conflict – retrying in 5 seconds...")
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(2)
        await asyncio.sleep(0.5)

# ====================== IMAGE PROXY FOR TELEGRAM ======================
@app.get("/telegram_image/{file_id}")
async def get_telegram_image(file_id: str):
    try:
        print(f"🔍 Received request for file_id: {file_id}")
        file = await bot.get_file(file_id)
        print(f"📁 File path: {file.file_path}")

        # Download as bytes
        file_bytes = await file.download_as_bytearray()
        file_bytes = bytes(file_bytes)  # convert to bytes for Response
        print(f"✅ Downloaded {len(file_bytes)} bytes")

        # Determine content type
        if file.file_path.endswith(".png"):
            content_type = "image/png"
        elif file.file_path.endswith((".jpg", ".jpeg")):
            content_type = "image/jpeg"
        elif file.file_path.endswith(".gif"):
            content_type = "image/gif"
        else:
            content_type = "image/jpeg"

        return Response(content=file_bytes, media_type=content_type)

    except Exception as e:
        print(f"❌ Error fetching image {file_id}: {e}")
        traceback.print_exc()
        return Response(status_code=404)

# ====================== STARTUP / SHUTDOWN ======================
@app.on_event("startup")
async def startup_event():
    global polling_task
    try:
        await asyncio.wait_for(bot.delete_webhook(), timeout=5.0)
        print("✅ Webhook deleted. Starting polling...")
    except asyncio.TimeoutError:
        print("⚠️ Webhook deletion timed out – will retry inside polling loop.")
    except Exception as e:
        print(f"⚠️ Could not delete webhook: {e}")

    polling_task = asyncio.create_task(telegram_polling())
    print("🚀 Server running with Telegram polling.")

@app.on_event("shutdown")
async def shutdown_event():
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except:
            pass
