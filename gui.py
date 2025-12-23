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
from PIL import Image, ImageTk
from pyzbar import pyzbar
import io
import hashlib
import threading
import queue
import tkinter as tk
from tkinter import ttk as tkttk
import requests

# 使用 ttkbootstrap 替代标准 ttk（更美观）
try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *
except ImportError:
    print("请先安装: pip install ttkbootstrap")
    sys.exit(1)

# ========== 全局配置 ==========
PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8080
SEND_INTERVAL = 15

MYSQL_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "123456",
    "db": "vmq_qr",
    "charset": "utf8mb4"
}

# 全局队列 & DB 连接池
image_queue = None
db_pool = None

# 用于 GUI 日志线程安全写入
log_queue = queue.Queue()

# ========== 工具函数 ==========


def extract_qr_content(image_bytes: bytes):
    try:
        image = Image.open(io.BytesIO(image_bytes))
        decoded_list = pyzbar.decode(image)
        if decoded_list:
            data = decoded_list[0].data
            try:
                return data.decode('utf-8')
            except UnicodeDecodeError:
                return data.hex()
        return None
    except Exception:
        return None


def get_md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


async def download_image(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                return await resp.read()
            else:
                raise Exception(f"HTTP {resp.status}")

# ========== 数据库操作 ==========


async def init_db():
    global db_pool
    db_pool = await aiomysql.create_pool(**MYSQL_CONFIG)


async def save_qrcode_if_new(url: str, qr_content: str, group_name: str, sender_name: str, log_func):
    qr_md5 = get_md5(qr_content)
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT IGNORE INTO qrcode_images 
                (url, qr_content, qr_md5, group_name, sender_name) 
                VALUES (%s, %s, %s, %s, %s)
            """, (url, qr_content, qr_md5, group_name, sender_name))
            await conn.commit()
            if cur.rowcount > 0:
                log_func(
                    f"💾 [{datetime.now().strftime('%H:%M:%S')}] 新二维码已存库（MD5: {qr_md5[:8]}...）")
                return True
            else:
                log_func(
                    f"⏭️ [{datetime.now().strftime('%H:%M:%S')}] 二维码内容已存在（MD5: {qr_md5[:8]}...）")
                return False


async def fetch_latest_images(limit=10):
    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT url, qr_content, group_name, sender_name, detected_at 
                FROM qrcode_images 
                ORDER BY detected_at DESC 
                LIMIT %s
            """, (limit,))
            return await cur.fetchall()

# ========== 图片处理工作线程 ==========


async def image_processor_worker(log_func):
    while True:
        try:
            task = await image_queue.get()
            url = task["url"]
            group_name = task["group_name"]
            sender_name = task["sender_name"]

            try:
                img_data = await download_image(url)
            except Exception as e:
                log_func(f"⚠️ 下载失败 {url}: {e}")
                image_queue.task_done()
                continue

            qr_content = extract_qr_content(img_data)
            if qr_content is None:
                log_func(f"🖼️ 非二维码（跳过）：{url}")
                image_queue.task_done()
                continue

            await save_qrcode_if_new(url, qr_content, group_name, sender_name, log_func)
            image_queue.task_done()

        except Exception as e:
            log_func(f"💥 图片处理异常: {e}")
            image_queue.task_done()

# ========== MITMProxy 插件 ==========


class WSSPeriodicSender:
    def __init__(self, log_func):
        self.log = log_func
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
                        self.log(
                            f"\n📩 [{datetime.now().strftime('%H:%M:%S')}] 收到群列表更新1")
                        data_list = json_data['r'][1]
                        
                        for v in data_list:
                            dict_json = {'name': v['3'],
                                         'id': v['1'], 't': '0'}
                            if not any(item['id'] == dict_json['id'] for item in self.qun_lists):
                                self.log(f"   ✨ 新增群：{dict_json}")
                                self.qun_lists.append(dict_json)
                            else:
                                self.log(f"   ℹ️  群已存在：{dict_json}")

                    if json_data.get('sid', 8) == 8 and json_data.get('cid', 0) == 23:
                        self.log(
                            f"\n📩 [{datetime.now().strftime('%H:%M:%S')}] 收到群列表更新2")
                        data_list = json_data['r'][0]
                        last_50 = data_list[-50:]
                        print('----消息last_50----', len(last_50), last_50)
                        for v in last_50:
                            if '图片' in v['17']:
                                name = v['6']
                                target_id = v['1']
                                qun_name = next(
                                    (item['name'] for item in self.qun_lists if item['id'] == target_id), None)
                                img_data = json.loads(v['10'])
                                img_url = img_data['url']

                                self.log(f"\nℹ️====收到图片消息==== [{datetime.now().strftime('%H:%M:%S')}] "
                                         f"群名: {qun_name} 昵称：{name} | 图片连接：{img_url}")

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
                            self.log(
                                f"⏰ [{datetime.now().strftime('%H:%M:%S')}] {SEND_INTERVAL}秒周期发送任务已启动")

            except json.JSONDecodeError:
                self.log(
                    f"❌ [{datetime.now().strftime('%H:%M:%S')}] JSON解析失败：{msg_content[:200]}")
            except Exception as e:
                self.log(
                    f"❌ [{datetime.now().strftime('%H:%M:%S')}] 消息处理异常：{e}")

    async def periodic_send_messages(self):
        while self.is_connected:
            try:
                if self.flow is None:
                    self.is_connected = False
                    self.log(
                        f"\n❌ [{datetime.now().strftime('%H:%M:%S')}] WSS连接已断开，停止发送")
                    break

                if self.qun_lists:
                    self.log(
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

                        # if "国彩大法师" in v['name']:
                        #     continue

                        send_content = f"3:::{json.dumps(send_json_data, ensure_ascii=False)}"
                        ctx.master.commands.call(
                            "inject.websocket",
                            self.flow,
                            False,
                            send_content.encode('utf-8')
                        )
                        self.log(f"✅ [{idx+1}/{len(self.qun_lists)}] 发送成功")
                        self.log(
                            f"----发送请求----群名：{v['name']} | SER：{self.ser} | 群ID：{v['id']}")

                await asyncio.sleep(SEND_INTERVAL)

            except asyncio.CancelledError:
                self.log(
                    f"\n🛑 [{datetime.now().strftime('%H:%M:%S')}] 周期发送任务已取消")
                break
            except Exception as e:
                self.log(
                    f"\n❌ [{datetime.now().strftime('%H:%M:%S')}] 周期发送异常：{e}")
                await asyncio.sleep(SEND_INTERVAL)

    def done(self):
        if self.send_task and not self.send_task.done():
            self.send_task.cancel()
        self.log(f"\n👋 [{datetime.now().strftime('%H:%M:%S')}] WSS周期发送代理已停止")


# ========== GUI 应用 ==========
class QRCodeApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="litera")
        self.title("二维码监控代理系统")
        self.geometry("1800x1000")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 控制状态
        self.proxy_running = False
        self.loop = None
        self.proxy_thread = None

        # 构建 UI
        self.build_ui()

        # 启动日志监听器（独立线程）
        self.log_listener = threading.Thread(
            target=self._log_consumer, daemon=True)
        self.log_listener.start()

    def build_ui(self):
        # === 顶部按钮区域 ===
        top_frame = ttk.Frame(self)
        top_frame.pack(fill=X, padx=10, pady=5)

        self.btn_start = ttk.Button(
            top_frame, text="▶ 启动代理", command=self.start_proxy, bootstyle=SUCCESS)
        self.btn_start.pack(side=LEFT, padx=5)

        self.btn_stop = ttk.Button(
            top_frame, text="⏹ 停止代理", command=self.stop_proxy, bootstyle=DANGER, state=DISABLED)
        self.btn_stop.pack(side=LEFT, padx=5)

        self.btn_refresh = ttk.Button(
            top_frame, text="🔄 刷新图片", command=self.load_images_from_db)
        self.btn_refresh.pack(side=LEFT, padx=5)

        # === 中部图片展示区域（带滚动）===
        mid_frame = ttk.LabelFrame(self, text="二维码图片库", padding=10)
        mid_frame.pack(fill=BOTH, expand=YES, padx=10, pady=5)

        canvas = tk.Canvas(mid_frame)
        scrollbar = tkttk.Scrollbar(
            mid_frame, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.image_labels = []

        # === 底部日志区域 ===
        log_frame = ttk.LabelFrame(self, text="实时日志", padding=5)
        log_frame.pack(fill=BOTH, expand=YES, padx=10, pady=5)

        self.log_text = tk.Text(log_frame, height=10,
                                wrap="word", font=("Consolas", 10))
        log_scroll = tkttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def gui_log(self, msg):
        """安全地将日志推送到队列"""
        log_queue.put(str(msg))

    def _log_consumer(self):
        """后台线程消费日志队列并更新 GUI"""
        while True:
            try:
                msg = log_queue.get(timeout=1)
                if msg == "__STOP__":
                    break
                self.after(0, lambda m=msg: self.log_text.insert(
                    tk.END, m + "\n"))
                self.after(0, lambda: self.log_text.see(tk.END))
            except:
                continue

    def start_proxy(self):
        if self.proxy_running:
            return
        self.proxy_running = True
        self.btn_start.config(state=DISABLED)
        self.btn_stop.config(state=NORMAL)

        # 初始化全局队列
        global image_queue
        image_queue = asyncio.Queue()

        # 在新线程中运行 asyncio loop
        self.proxy_thread = threading.Thread(
            target=self._run_proxy_in_thread, daemon=True)
        self.proxy_thread.start()

    def _run_proxy_in_thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start_proxy_async())
        except Exception as e:
            self.gui_log(f"[FATAL] 代理崩溃: {e}")
        finally:
            self.proxy_running = False
            self.after(0, lambda: self.btn_start.config(state=NORMAL))
            self.after(0, lambda: self.btn_stop.config(state=DISABLED))

    async def _start_proxy_async(self):
        await init_db()
        asyncio.create_task(image_processor_worker(self.gui_log))

        opts = options.Options(
            listen_host=PROXY_HOST,
            listen_port=PROXY_PORT,
            ssl_insecure=True
        )
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        sender = WSSPeriodicSender(self.gui_log)
        master.addons.add(sender)

        self.gui_log("="*60)
        self.gui_log(f"✅ proxy已启动")
        self.gui_log(f"📌 监听地址：http://{PROXY_HOST}:{PROXY_PORT}")
        self.gui_log(f"📌 请求间隔：{SEND_INTERVAL}秒")
        self.gui_log("="*60)

        await master.run()

    def stop_proxy(self):
        if self.loop and self.proxy_running:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.proxy_running = False
        self.btn_start.config(state=NORMAL)
        self.btn_stop.config(state=DISABLED)
        self.gui_log("\n🛑 代理已停止\n")

    def load_images_from_db(self):
        if not db_pool:
            self.gui_log("⚠️ 数据库未初始化")
            return

        def _fetch_and_update():
            async def _inner():
                try:
                    records = await fetch_all_images()
                    self.after(0, self._update_image_display, records)
                except Exception as e:
                    self.after(0, self.gui_log, f"❌ 加载图片失败: {e}")

            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(_inner(), self.loop)
            else:
                self.gui_log("警告：主事件循环未运行或不存在")

        threading.Thread(target=_fetch_and_update, daemon=True).start()

    def _update_image_display(self, records):
        # 清空旧图
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        col = 0
        row = 0
        for rec in records:
            print('----rec----', rec)
            try:
                # 下载图片

                img_data = requests.get(rec['url'],  proxies={
                                        "http": None, "https": None}, timeout=10).content
                img = Image.open(io.BytesIO(img_data))
                img.thumbnail((200, 200))  # 调整尺寸大小
                photo = ImageTk.PhotoImage(img)

                frame = ttk.Frame(self.scrollable_frame, padding=5)
                label_img = ttk.Label(frame, image=photo)
                label_img.image = photo  # 防止被回收
                label_img.pack()

                info = f"{rec['group_name']} | {rec['sender_name']}"
                ttk.Label(frame, text=info[:30], font=("Arial", 8)).pack()
                frame.grid(row=row, column=col, padx=5, pady=5)

                col += 1
                if col >= 4:  # 每行展示4张图片
                    col = 0
                    row += 1

            except Exception as e:
                self.gui_log(f"跳过损坏或无法加载的图片: {e}")
                continue  # 跳过损坏图片或加载失败的情况

    def on_closing(self):
        self.stop_proxy()
        log_queue.put("__STOP__")
        self.destroy()


async def fetch_all_images():
    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT url, qr_content, group_name, sender_name, detected_at 
                FROM qrcode_images 
                ORDER BY detected_at DESC
            """)
            return await cur.fetchall()


# ========== 入口 ==========
if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 初始化数据库并启动 GUI
    async def init_and_run():
        global db_pool
        db_pool = await aiomysql.create_pool(**MYSQL_CONFIG)
        app = QRCodeApp()
        app.mainloop()  # 注意：mainloop() 是阻塞的，不会返回

    asyncio.run(init_and_run())
    # 不要再写 app = QRCodeApp() 了！
