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
from concurrent.futures import ThreadPoolExecutor
import weakref

# 全局线程池（避免频繁创建）
IMAGE_THREAD_POOL = ThreadPoolExecutor(max_workers=10)

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
    # "host": "8.217.1.0",
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


async def save_qrcode_if_new(url: str, qr_content: str, group_name: str, sender_name: str, log_func, app_instance):
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
                    f"💾 [{datetime.now().strftime('%H:%M:%S')}] 新二维码已存库（标识: {qr_md5[:8]}...）")
                # 刷新图片显示
                app_instance.refresh_images()
            else:
                # print(f"⏭️ [{datetime.now().strftime('%H:%M:%S')}] 二维码内容已存在（MD5: {qr_md5[:8]}...）")
                log_func(
                    f"⏭️ [{datetime.now().strftime('%H:%M:%S')}] 二维码内容已存在（标识: {qr_md5[:8]}...）")

async def fetch_latest_images(limit=999):
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


async def image_processor_worker(log_func,app_instance):
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
                # print(f"🖼️ 非二维码（跳过）：{url}")
                # log_func(f"🖼️ 非二维码（跳过）：{url}")
                image_queue.task_done()
                continue

            await save_qrcode_if_new(url, qr_content, group_name, sender_name, log_func,app_instance)
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
                        last_50 = data_list[-30:]
                        # print('----消息last_50----', len(last_50), last_50)
                        for v in last_50:
                            if '图片' in v['17']:
                                name = v['6']
                                target_id = v['1']
                                qun_name = next(
                                    (item['name'] for item in self.qun_lists if item['id'] == target_id), None)
                                img_data = json.loads(v['10'])
                                img_url = img_data['url']

                                # self.log(f"\nℹ️====收到图片消息==== [{datetime.now().strftime('%H:%M:%S')}] "
                                #          f"群名: {qun_name} 昵称：{name} | 图片连接：{img_url}")

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
                self.log(f"监听中...")
                await asyncio.sleep(SEND_INTERVAL)

            except asyncio.CancelledError:
                self.log(
                    f"\n🛑 [{datetime.now().strftime('%H:%M:%S')}] 周期发送任务已取消")
                break
            except Exception as e:
                self.log(
                    f"\n❌ [{datetime.now().strftime('%H:%M:%S')}] 周期发送异常：{e}")
                self.log(f"监听中...")
                await asyncio.sleep(SEND_INTERVAL)
                

    def done(self):
        if self.send_task and not self.send_task.done():
            self.send_task.cancel()
        self.log(f"\n👋 [{datetime.now().strftime('%H:%M:%S')}] WSS周期发送代理已停止")


# ========== GUI 应用 ==========
class QRCodeApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="litera")
        self.title("vmq二维码监控系统")
        self.geometry("1800x1000")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 控制状态
        self.proxy_running = False
        self.loop = None
        self.proxy_thread = None

        # 控制状态
        self.proxy_running = False
        self.loop = None
        self.proxy_thread = None
        self.last_update_time = None  # 用于自动刷新判断
        self.loading_label = None     # 加载提示标签

        # 构建 UI
        self.build_ui()

        # 启动日志监听器（独立线程）
        self.log_listener = threading.Thread(
            target=self._log_consumer, daemon=True)
        self.log_listener.start()

        # 自动加载数据库中的图片
        self.after(100, self.load_images_from_db)  # 在UI构建完成后稍后调用

        # 启动自动刷新协程（需在 asyncio loop 中）
        # self.after(200, self._start_auto_refresh)  # 稍后启动

        self.after(50, self._init_last_update_time)
        

    def refresh_images(self):
        """刷新图片显示的方法"""
        self.load_images_from_db()

    def _init_last_update_time(self):
        """启动时从数据库获取最新 detected_at 作为初始时间戳"""
        if not db_pool:
            return

        def _get_latest():
            async def _inner():
                try:
                    async with db_pool.acquire() as conn:
                        async with conn.cursor(aiomysql.DictCursor) as cur:
                            await cur.execute("SELECT MAX(detected_at) as latest FROM qrcode_images")
                            res = await cur.fetchone()
                            self.last_update_time = res['latest'] if res and res['latest'] else datetime.min
                            self.gui_log(
                                f"🕒 初始时间戳已设置: {self.last_update_time}")
                except Exception as e:
                    self.gui_log(f"⚠️ 初始化 last_update_time 失败: {e}")

            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(_inner(), self.loop)

        threading.Thread(target=_get_latest, daemon=True).start()

    def _load_image_async(self, rec, placeholder, frame):
        url = rec['url']

        async def fetch_image():
            try:
                img_data = await download_image(url)
                img = Image.open(io.BytesIO(img_data))
                img_thumb = img.resize((100, 100), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img_thumb)
                # 传 url 而不是 img
                self.after(0, lambda: self._show_image(
                    placeholder, photo, url, rec))
            except Exception as e:
                self.gui_log(f"下载或处理图片失败 {url}: {e}")

        asyncio.run_coroutine_threadsafe(fetch_image(), self.loop)

    # def _start_auto_refresh(self):
    #     """启动后台自动刷新（每10秒检查一次）"""
    #     if not self.loop or not self.loop.is_running():
    #         return

    #     async def _auto_refresh_loop(self):
    #         while True:
    #             try:
    #                 if db_pool:  # 不再依赖 proxy_running
    #                     async with db_pool.acquire() as conn:
    #                         async with conn.cursor(aiomysql.DictCursor) as cur:
    #                             await cur.execute("SELECT MAX(detected_at) as latest FROM qrcode_images")
    #                             result = await cur.fetchone()
    #                             latest = result['latest'] if result and result['latest'] else None

    #                     if latest and (not self.last_update_time or latest > self.last_update_time):
    #                         self.gui_log("🆕 检测到新二维码，自动刷新...")
    #                         self.load_images_from_db()
    #                         self.last_update_time = latest
    #                 await asyncio.sleep(10)
    #             except Exception as e:
    #                 self.gui_log(f"自动刷新异常: {e}")
    #                 await asyncio.sleep(10)

    #     # 启动协程
    #     asyncio.run_coroutine_threadsafe(_auto_refresh_loop(), self.loop)

    def build_ui(self):
        # === 顶部按钮区域 ===
        top_frame = ttk.Frame(self)
        top_frame.pack(fill=X, padx=10, pady=5)

        self.btn_start = ttk.Button(
            top_frame, text="▶ 开始运行", command=self.start_proxy, bootstyle=SUCCESS)
        self.btn_start.pack(side=LEFT, padx=5)

        self.btn_stop = ttk.Button(
            top_frame, text="⏹ 停止运行", command=self.stop_proxy, bootstyle=DANGER, state=DISABLED)
        self.btn_stop.pack(side=LEFT, padx=5)

        self.btn_refresh = ttk.Button(
            top_frame, text="🔄 查看|刷新二维码图片库", command=self.load_images_from_db)
        self.btn_refresh.pack(side=LEFT, padx=5)

        # === 中部图片展示区域（横向滚动）===
        mid_frame = ttk.LabelFrame(self, text="二维码图片库", padding=10)
        mid_frame.pack(fill=BOTH, expand=YES, padx=10, pady=5)

        # 创建带滚动条的 Canvas
        self.canvas = tk.Canvas(mid_frame, bg='white')
        v_scrollbar = tkttk.Scrollbar(
            mid_frame, orient="vertical", command=self.canvas.yview)
        h_scrollbar = tkttk.Scrollbar(
            mid_frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=v_scrollbar.set,
                              xscrollcommand=h_scrollbar.set)

        # 内容容器
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.canvas.create_window(
            (0, 0), window=self.scrollable_frame, anchor="nw")

        # 布局
        self.canvas.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")
        mid_frame.grid_rowconfigure(0, weight=1)
        mid_frame.grid_columnconfigure(0, weight=1)

        # 绑定滚轮（Windows & macOS）
        def _on_mousewheel(event):
            if event.delta:
                self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            else:
                self.canvas.yview_scroll(-1 if event.num == 5 else 1, "units")
        self.canvas.bind("<MouseWheel>", _on_mousewheel)  # Windows
        self.canvas.bind("<Button-4>", _on_mousewheel)    # Linux up
        self.canvas.bind("<Button-5>", _on_mousewheel)    # Linux down

        # 更新 scrollregion
        def _configure_scrollable(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.scrollable_frame.bind("<Configure>", _configure_scrollable)

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
        asyncio.create_task(image_processor_worker(self.gui_log,self))

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
        # self.refresh_images(self)

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

        if self.loading_label is None:
            self.loading_label = ttk.Label(
                self.scrollable_frame, text="⏳ 加载中...", font=("Arial", 14))
            self.loading_label.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.loading_label.lift()

        def _fetch_and_update():
            async def _inner():
                try:
                    records = await fetch_all_images()
                    if records:
                        # 更新为最新一条的时间
                        self.last_update_time = max(
                            rec['detected_at'] for rec in records if rec['detected_at']
                        )
                    else:
                        self.last_update_time = datetime.min
                    self.after(0, self._update_image_display, records)
                except Exception as e:
                    self.after(0, self.gui_log, f"❌ 加载图片失败: {e}")
                finally:
                    self.after(0, self._hide_loading)

            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(_inner(), self.loop)
            else:
                self.gui_log("警告：主事件循环未运行")

        threading.Thread(target=_fetch_and_update, daemon=True).start()

    def _hide_loading(self):
        if self.loading_timer:
            self.loading_timer.cancel()
        if self.loading_label:
            self.loading_label.place_forget()
            self.loading_label = None

    def _update_image_display(self, records):
        # 清空旧内容
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        self.loading_label = None

        count = len(records)  # 计算图片数量
        if count == 0:
            ttk.Label(self.scrollable_frame, text="暂无二维码图片",
                      font=("Arial", 12)).pack(pady=20)
        else:
            ttk.Label(self.scrollable_frame, text=f"共 {count} 张图片", font=(
                "Arial", 12)).pack(pady=5)

        # 创建一个新的Frame用于grid布局
        grid_container = ttk.Frame(self.scrollable_frame)
        grid_container.pack(fill=tk.BOTH, expand=True)

        col = 0
        row = 0
        for rec in records:
            try:
                frame = ttk.Frame(grid_container, padding=5)

                # 占位图（防止布局抖动）
                placeholder = ttk.Label(
                    frame, text="加载中...", width=25, anchor="center")
                placeholder.grid(row=0, column=0, sticky="nsew")

                # 异步加载图片（不阻塞 GUI）
                self._load_image_async(rec, placeholder, frame)

                # 显示群名和时间
                group_name = rec.get('group_name', '未知群')
                detected_at = rec.get('detected_at')
                time_str = detected_at.strftime(
                    "%m-%d %H:%M") if detected_at else "未知时间"
                info = f"{group_name} | {time_str}"
                ttk.Label(frame, text=info[:30], font=("Arial", 8)).grid(
                    row=1, column=0, sticky="nsew")

                frame.grid(row=row, column=col, padx=3, pady=3, sticky="nsew")
                col += 1
                if col >= 10:  # 一行10张
                    col = 0
                    row += 1

            except Exception as e:
                self.gui_log(f"构建图片项失败: {e}")
                continue

        # 更新滚动区域
        self.after(100, lambda: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))

    def _show_image(self, placeholder, photo, url, rec):  # 注意：这里 img 改成 url
        placeholder.config(image=photo, text="")
        placeholder.image = photo

        def on_click(event):
            self._show_full_image(url, rec)  # 传 url
        placeholder.bind("<Button-1>", on_click)
        placeholder.config(cursor="hand2")


    def _show_full_image(self, url, rec):
        """弹出新窗口，从 URL 下载原图并显示"""
        top = tk.Toplevel(self)
        top.title(f"二维码预览 - {rec.get('group_name', '')}")
        top.geometry("0x0")  # 初始大小
        top.resizable(True, True)

        # 显示加载中
        label = ttk.Label(top, text="正在加载原图...", font=("Arial", 12))
        label.pack(expand=True)

        def _download_and_show():
            try:
                # 同步下载（在子线程）
                response = requests.get(
                    url, proxies={"http": None, "https": None}, timeout=10)
                response.raise_for_status()
                img = Image.open(io.BytesIO(response.content))

                # 计算缩放尺寸（最大 1200x1200）
                width, height = img.size
                max_size = 1200
                if width > max_size or height > max_size:
                    scale = max_size / max(width, height)
                    new_w, new_h = int(width * scale), int(height * scale)
                else:
                    new_w, new_h = width, height

                # 调整窗口大小
                x = (top.winfo_screenwidth() - new_w) // 2
                y = (top.winfo_screenheight() - new_h) // 2
                top.geometry(f"{new_w+200}x{new_h + 200}+{x}+{y}")

                # 缩放图片（保持清晰）
                img_resized = img.resize(
                    (new_w, new_h), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img_resized)

                # 更新 Label
                label.config(image=photo, text="")
                label.image = photo  # 防止回收

                # 添加信息
                info = f"群: {rec.get('group_name', 'N/A')} | 发送者: {rec.get('sender_name', 'N/A')} | 时间： {rec.get('detected_at', 'N/A')}"
                ttk.Label(top, text=info, font=("Arial", 10)).pack(pady=5)

            except Exception as e:
                label.config(text=f"❌ 加载失败: {e}")

        # 在后台线程下载，避免卡死 GUI
        threading.Thread(target=_download_and_show, daemon=True).start()

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
        global db_pool,app
        db_pool = await aiomysql.create_pool(**MYSQL_CONFIG)
        app = QRCodeApp()
        app.mainloop()  # 注意：mainloop() 是阻塞的，不会返回

    asyncio.run(init_and_run())
    # 不要再写 app = QRCodeApp() 了！
