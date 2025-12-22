"""
mitmproxy 8.0 周期发送WS消息 + 异步识别二维码图片存MySQL
核心：异步周期任务 + 图片异步识别 + MySQL去重存储 + 纯净日志
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from mitmproxy import options, http, ctx
from mitmproxy.tools.dump import DumpMaster
import sys
import aiohttp
import aiomysql
from PIL import Image
from pyzbar import pyzbar
import io
import hashlib
from pyzbar import pyzbar
from PIL import Image
import io

# ========== 全局日志配置 ==========
logging.getLogger("mitmproxy").setLevel(logging.CRITICAL)
logging.getLogger("mitmproxy.http").setLevel(logging.CRITICAL)
logging.getLogger("mitmproxy.websocket").setLevel(logging.CRITICAL)
logging.getLogger("mitmproxy.master").setLevel(logging.CRITICAL)
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

# 代理配置
PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8080
# 发送间隔：30秒
SEND_INTERVAL = 30

# MySQL 配置（请按实际修改）
MYSQL_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "123456",
    "db": "vmq_qr",
    "charset": "utf8mb4"
}

# 全局队列 & DB 连接池
image_queue = asyncio.Queue()
db_pool = None


async def init_db():
    global db_pool
    db_pool = await aiomysql.create_pool(**MYSQL_CONFIG)


def extract_qr_content(image_bytes: bytes):
    """从图片中提取二维码内容（返回第一个）"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        decoded_list = pyzbar.decode(image)
        if decoded_list:
            # 取第一个二维码的内容（bytes），转为字符串
            data = decoded_list[0].data
            # 尝试按 UTF-8 解码，失败则保留原始 bytes 的 hex
            try:
                return data.decode('utf-8')
            except UnicodeDecodeError:
                return data.hex()  # 或 base64.b64encode(data).decode()
        return None
    except Exception:
        return None


def get_md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


async def save_qrcode_if_new(url: str, qr_content: str, group_name: str, sender_name: str):
    qr_md5 = get_md5(qr_content)
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            # 使用 INSERT IGNORE 或 ON DUPLICATE KEY UPDATE
            await cur.execute("""
                INSERT IGNORE INTO qrcode_images 
                (url, qr_content, qr_md5, group_name, sender_name) 
                VALUES (%s, %s, %s, %s, %s)
            """, (url, qr_content, qr_md5, group_name, sender_name))
            await conn.commit()
            if cur.rowcount > 0:
                print(
                    f"💾 [{datetime.now().strftime('%H:%M:%S')}] 新二维码已存库（MD5: {qr_md5[:8]}...）")
                return True
            else:
                print(
                    f"⏭️ [{datetime.now().strftime('%H:%M:%S')}] 二维码内容已存在（MD5: {qr_md5[:8]}...）")
                return False


async def is_url_exists(url: str) -> bool:
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM qrcode_images WHERE url = %s LIMIT 1", (url,))
            return await cur.fetchone() is not None


async def save_qrcode_image(url: str, group_name: str, sender_name: str):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO qrcode_images (url, group_name, sender_name) VALUES (%s, %s, %s)",
                (url, group_name, sender_name)
            )
            await conn.commit()
            if cur.rowcount > 0:
                print(f"💾 [{datetime.now().strftime('%H:%M:%S')}] 二维码已存库：{url}")


async def download_image(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                return await resp.read()
            else:
                raise Exception(f"HTTP {resp.status}")


def is_qr_code(image_bytes: bytes) -> bool:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        decoded = pyzbar.decode(image)
        return len(decoded) > 0
    except Exception:
        return False


async def image_processor_worker():
    while True:
        try:
            task = await image_queue.get()
            url = task["url"]
            group_name = task["group_name"]
            sender_name = task["sender_name"]

            # 下载图片
            try:
                img_data = await download_image(url)
            except Exception as e:
                print(f"⚠️ 下载失败 {url}: {e}")
                image_queue.task_done()
                continue

            # 提取二维码内容
            qr_content = extract_qr_content(img_data)
            if qr_content is None:
                print(f"🖼️ 非二维码（跳过）：{url}")
                image_queue.task_done()
                continue

            # 保存（自动去重）
            await save_qrcode_if_new(url, qr_content, group_name, sender_name)

            image_queue.task_done()

        except Exception as e:
            print(f"💥 图片处理异常: {e}")
            image_queue.task_done()


class WSSPeriodicSender:
    def __init__(self):
        self.qun_lists = []
        self.ser = 0
        self.flow = None
        self.send_task = None
        self.is_connected = False

    def websocket_message(self, flow: http.HTTPFlow):
        assert flow.websocket is not None
        last_message = flow.websocket.messages[-1]

        if (not last_message.from_client and last_message.is_text and
                "weblink.netease.im/socket.io/1/websocket/" in flow.request.url):

            msg_content = last_message.content.decode("utf-8", errors="ignore")

            try:
                json_data = json.loads(msg_content[4:])
                code = json_data.get('code', -1)

                if code == 200:
                    self.ser = json_data.get('ser', 0)

                    if json_data.get('sid', 0) == 8 and json_data.get('cid', 0) == 109:
                        print(
                            f"\n📩 [{datetime.now().strftime('%H:%M:%S')}] 收到群列表更新1")
                        data_list = json_data['r'][1]
                        for v in data_list:
                            dict_json = {'name': v['3'],
                                         'id': v['1'], 't': '0'}
                            if not any(item['id'] == dict_json['id'] for item in self.qun_lists):
                                print(f"   ✨ 新增群：{dict_json}")
                                self.qun_lists.append(dict_json)
                            else:
                                print(f"   ℹ️  群已存在：{dict_json}")

                    if json_data.get('sid', 8) == 8 and json_data.get('cid', 0) == 23:
                        print(
                            f"\n📩 [{datetime.now().strftime('%H:%M:%S')}] 收到群列表更新2")
                        data_list = json_data['r'][0]
                        for v in data_list:
                            if '图片' in v['17']:
                                name = v['6']
                                target_id = v['1']
                                qun_name = next(
                                    (item['name'] for item in self.qun_lists if item['id'] == target_id), None)
                                img_data = json.loads(v['10'])
                                img_url = img_data['url']

                                print(f"\nℹ️====收到图片消息==== [{datetime.now().strftime('%H:%M:%S')}] "
                                      f"群名: {qun_name} 昵称：{name} | 图片连接：{img_url}")

                                # 👇 异步提交图片检测任务（非阻塞！）
                                asyncio.create_task(image_queue.put({
                                    "url": img_url,
                                    "group_name": qun_name or "未知群",
                                    "sender_name": name
                                }))

                                if qun_name:
                                    for item in self.qun_lists:
                                        if item['id'] == target_id:
                                            item['t'] = data_list[-1]['12']
                                            break

                    if self.ser == 3 or not self.is_connected:
                        self.flow = flow
                        self.is_connected = True
                        if self.send_task is None or self.send_task.done():
                            self.send_task = asyncio.create_task(
                                self.periodic_send_messages())
                            print(
                                f"⏰ [{datetime.now().strftime('%H:%M:%S')}] {SEND_INTERVAL}秒周期发送任务已启动")

            except json.JSONDecodeError:
                print(
                    f"❌ [{datetime.now().strftime('%H:%M:%S')}] JSON解析失败：{msg_content[:200]}")
            except Exception as e:
                print(f"❌ [{datetime.now().strftime('%H:%M:%S')}] 消息处理异常：{e}")

    async def periodic_send_messages(self):
        while self.is_connected:
            try:
                if self.flow is None:
                    self.is_connected = False
                    print(
                        f"\n❌ [{datetime.now().strftime('%H:%M:%S')}] WSS连接已断开，停止发送")
                    break

                if self.qun_lists:
                    print(
                        f"\n🚀 [{datetime.now().strftime('%H:%M:%S')}] 开始执行周期发送（间隔{SEND_INTERVAL}秒）")
                    for idx, v in enumerate(self.qun_lists):
                        self.ser += 1
                        send_json_data = {
                            "SID": 8,
                            "CID": 23,
                            "SER": self.ser,
                            "Q": [
                                {"t": "long", "v": v['id']},
                                {"t": "long", "v": 0},
                                {"t": "long", "v": int(time.time() * 1000)},
                                {"t": "long", "v": v['t']},
                                {"t": "int", "v": 100},
                                {"t": "bool", "v": "false"},
                                {"t": "LongArray", "v": [100]}
                            ]
                        }

                        # 只输出自己的群
                        if "国彩大法师" in v['name']:
                            continue

                        send_content = f"3:::{json.dumps(send_json_data, ensure_ascii=False)}"
                        ctx.master.commands.call(
                            "inject.websocket",
                            self.flow,
                            False,
                            send_content.encode('utf-8')
                        )
                        print(f"✅ [{idx+1}/{len(self.qun_lists)}] 发送成功")
                        print(
                            f"----发送请求----群名：{v['name']} | SER：{self.ser} | 群ID：{v['id']}")

                await asyncio.sleep(SEND_INTERVAL)

            except asyncio.CancelledError:
                print(f"\n🛑 [{datetime.now().strftime('%H:%M:%S')}] 周期发送任务已取消")
                break
            except Exception as e:
                print(
                    f"\n❌ [{datetime.now().strftime('%H:%M:%S')}] 周期发送异常：{e}")
                await asyncio.sleep(SEND_INTERVAL)

    def done(self):
        if self.send_task and not self.send_task.done():
            self.send_task.cancel()
        print(f"\n👋 [{datetime.now().strftime('%H:%M:%S')}] WSS周期发送代理已停止")


async def start_proxy():
    global db_pool
    await init_db()

    # 启动后台图片处理器
    asyncio.create_task(image_processor_worker())

    opts = options.Options(
        listen_host=PROXY_HOST,
        listen_port=PROXY_PORT,
        ssl_insecure=True
    )

    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    sender = WSSPeriodicSender()
    master.addons.add(sender)

    print("="*60)
    print(f"✅ mitmproxy 8.0 WSS周期发送代理已启动")
    print(f"📌 监听地址：http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"📌 发送间隔：{SEND_INTERVAL}秒")
    print(f"📌 二维码图片将异步存入MySQL（自动去重）")
    print("="*60)

    await master.run()


if __name__ == "__main__":
    # 安装所需包（首次运行前）：
    # pip install mitmproxy aiohttp aiomysql Pillow pyzbar

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(start_proxy())
    except KeyboardInterrupt:
        print("\n\n🛑 代理被手动终止")
