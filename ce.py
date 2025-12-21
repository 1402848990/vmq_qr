"""
mitmproxy 8.0 周期发送WS消息（5秒一次 + 仅输出业务日志）
核心：异步周期任务 + 避免死循环 + 纯净业务日志
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from mitmproxy import options, http, ctx
from mitmproxy.tools.dump import DumpMaster
import sys
from mitmproxy.websocket import WebSocketMessage

# ========== 全局日志配置（屏蔽所有冗余日志） ==========
logging.getLogger("mitmproxy").setLevel(logging.CRITICAL)
logging.getLogger("mitmproxy.http").setLevel(logging.CRITICAL)
logging.getLogger("mitmproxy.websocket").setLevel(logging.CRITICAL)
logging.getLogger("mitmproxy.master").setLevel(logging.CRITICAL)
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
# =====================================================

# 代理配置
PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8080
SEND_INTERVAL = 30  # 发送间隔：5秒

class WSSPeriodicSender:
    def __init__(self):
        self.qun_lists = []
        self.ser = 0
        self.flow = None  # 保存目标WSS连接的flow
        self.send_task = None  # 周期发送任务
        self.is_connected = False  # 标记是否已建立有效连接
    #
    # def websocket_handshake(self, flow: http.HTTPFlow):
    #     """WSS握手成功时初始化连接（避免重复创建任务）"""
    #     print(flow.request.url)
    #     if "weblink.netease.im/socket.io/1/websocket/" in flow.websocket.url:
    #         # 保存有效连接的flow
    #         self.flow = flow
    #         self.is_connected = True
    #         print(f"\n🔌 [{datetime.now().strftime('%H:%M:%S')}] WSS连接建立成功")
    #
    #         # 启动周期发送任务（仅启动一次）
    #         if self.send_task is None or self.send_task.done():
    #             self.send_task = asyncio.create_task(self.periodic_send_messages())
    #             print(f"⏰ [{datetime.now().strftime('%H:%M:%S')}] 5秒周期发送任务已启动")

    def websocket_message(self, flow: http.HTTPFlow):
        """仅处理服务端消息，更新SER值和群列表（不触发发送）"""
        assert flow.websocket is not None
        last_message = flow.websocket.messages[-1]

        # 仅处理目标WSS连接的服务端文本消息
        if (not last_message.from_client and last_message.is_text and
            "weblink.netease.im/socket.io/1/websocket/" in flow.request.url):

            msg_content = last_message.content.decode("utf-8", errors="ignore")

            try:
                # 解析Socket.IO消息，更新业务数据
                json_data = json.loads(msg_content[4:])
                code = json_data.get('code', -1)

                if code == 200:
                    # 同步SER值
                    self.ser = json_data.get('ser', 0)

                    # 更新群列表（按需）
                    if json_data.get('sid', 0) == 8 and json_data.get('cid', 0) == 109:
                        print(f"\n📩 [{datetime.now().strftime('%H:%M:%S')}] 收到群列表更新")
                        data_list = json_data['r'][1]
                        for v in data_list:
                            dict_json = {'name': v['3'], 'id': v['1'], 't': '0'}
                            is_exist = any(item['id'] == dict_json['id'] for item in self.qun_lists)
                            if not is_exist:
                                print(f"   ✨ 新增群：{dict_json}")
                                self.qun_lists.append(dict_json)
                            else:
                                print(f"   ℹ️  群已存在：{dict_json}")

                    if json_data.get('sid', 8) == 8 and json_data.get('cid', 0) == 23:
                        # 开始解读消息
                        print(f"\n📩 [{datetime.now().strftime('%H:%M:%S')}] 收到群列表更新")
                        data_list = json_data['r'][0]
                        qun_name = None
                        for v in data_list:
                            if '图片' in v['17']:
                                name = v['6']
                                target_id = v['1']
                                if qun_name is None:
                                    qun_name = next((item['name'] for item in self.qun_lists if item['id'] == target_id), None)
                                img_data = json.loads(v['10'])
                                img_url = img_data['url']
                                print(f"\nℹ️ [{datetime.now().strftime('%H:%M:%S')}] 群名: {qun_name} 昵称：{name} | 图片连接：{img_url}")
                        if qun_name:
                            for item in self.qun_lists:
                                if item['id'] == target_id:
                                    item['t'] = data_list[-1]['12']  # 直接修改t字段
                                    break  # 找到后退出循环，提升效率
                    # print(f"\nℹ️ [{datetime.now().strftime('%H:%M:%S')}] 当前SER值：{self.ser} | 群数量：{len(self.qun_lists)}")
                    if self.ser == 3 or self.is_connected == False:
                        self.flow = flow
                        self.is_connected = True
                        # 启动周期发送任务（仅启动一次）
                        if self.send_task is None or self.send_task.done():
                            self.send_task = asyncio.create_task(self.periodic_send_messages())
                            print(f"⏰ [{datetime.now().strftime('%H:%M:%S')}] {SEND_INTERVAL}秒周期发送任务已启动")

            except json.JSONDecodeError:
                print(f"❌ [{datetime.now().strftime('%H:%M:%S')}] JSON解析失败：{msg_content[:200]}")
            except Exception as e:
                print(f"❌ [{datetime.now().strftime('%H:%M:%S')}] 消息处理异常：{e}")

    async def periodic_send_messages(self):
        """周期发送消息核心逻辑（5秒一次）"""
        while self.is_connected:
            try:
                # 检查连接有效性
                if (self.flow is None):
                    self.is_connected = False
                    print(f"\n❌ [{datetime.now().strftime('%H:%M:%S')}] WSS连接已断开，停止发送")
                    break

                # 执行群发逻辑
                if self.qun_lists and len(self.qun_lists) > 0:
                    print(f"\n🚀 [{datetime.now().strftime('%H:%M:%S')}] 开始执行周期发送（间隔{SEND_INTERVAL}秒）")
                    for idx, v in enumerate(self.qun_lists):
                        self.ser += 1
                        # 构造发送消息
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
                        send_content = f"3:::{json.dumps(send_json_data, ensure_ascii=False)}"

                        # 官方inject命令发送（客户端→服务端）
                        ctx.master.commands.call(
                            "inject.websocket",
                            self.flow,
                            False,  # ✅ 正确：客户端→服务端
                            send_content.encode('utf-8')
                        )
                        print(f"✅ [{idx+1}/{len(self.qun_lists)}] 发送成功")
                        print(f"   群名：{v['name']} | SER：{self.ser} | 群ID：{v['id']}")
                        print(f"   消息内容：{send_content[:200]}...")

                # 等待指定间隔（核心：避免死循环，严格5秒一次）
                await asyncio.sleep(SEND_INTERVAL)

            except asyncio.CancelledError:
                print(f"\n🛑 [{datetime.now().strftime('%H:%M:%S')}] 周期发送任务已取消")
                break
            except Exception as e:
                print(f"\n❌ [{datetime.now().strftime('%H:%M:%S')}] 周期发送异常：{e}")
                await asyncio.sleep(SEND_INTERVAL)  # 异常仍保持5秒间隔

    def done(self):
        """代理停止时清理任务"""
        if self.send_task and not self.send_task.done():
            self.send_task.cancel()
        print(f"\n👋 [{datetime.now().strftime('%H:%M:%S')}] WSS周期发送代理已停止")

# 启动代理入口
async def start_proxy():
    opts = options.Options(
        listen_host=PROXY_HOST,
        listen_port=PROXY_PORT,
        ssl_insecure=True
    )

    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    sender = WSSPeriodicSender()
    master.addons.add(sender)

    # 仅输出启动提示
    print("="*60)
    print(f"✅ mitmproxy 8.0 WSS周期发送代理已启动")
    print(f"📌 监听地址：http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"📌 发送间隔：{SEND_INTERVAL}秒")
    print("="*60)

    await master.run()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(start_proxy())
    except KeyboardInterrupt:
        print("\n\n🛑 代理被手动终止")