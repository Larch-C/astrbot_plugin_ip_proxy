# -*- coding: utf-8 -*-
import asyncio
import aiohttp
import time
import re
import json
from datetime import date
from pathlib import Path
from asyncio import StreamReader, StreamWriter, Task, Server

from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register, StarTools # [修改 1] 导入 StarTools
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult

# 使用 @register 装饰器注册插件
@register(
    "astrbot_plugin_ip_proxy",
    "timetetng",
    "一个将HTTP代理API转换为本地代理的AstrBot插件 ",
    "1.5",
    "https://github.com/timetetng/astrbot_plugin_ip_proxy"
)
class IPProxyPlugin(Star):
    """
    IP代理插件主类。
    采用 AstrBot 配置系统，通过API获取HTTP代理IP，并在本地启动一个代理服务。
    用户可以通过独立的指令来控制和配置代理服务。
    所有指令均需要管理员权限。
    """
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config: AstrBotConfig = config
        self.server: Server | None = None
        self.server_task: Task | None = None
        self.current_ip: str | None = None
        self.current_port: int | None = None
        self.last_validation_time: float | None = None
        self.ip_lock = asyncio.Lock()
        self.stats_lock = asyncio.Lock() # 用于保护统计数据并发读写的锁
        self.http_session = aiohttp.ClientSession()
        
        # --- 数据持久化设置 ---
        # [修改 1] 使用 StarTools 获取插件专属数据目录
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_ip_proxy") 
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.stats_file = self.data_dir / "stats.json"
        self.stats = {}
        # 在异步的 __init__ 中加载数据需要创建一个任务，或者在一个同步方法中加载
        # 为了简单起见，我们将在插件启动时同步加载一次
        self._load_stats_sync()
        
        logger.info("IP代理插件: 插件已加载，配置已注入。")

        if self.config.get("start_on_load", True):
            logger.info("IP代理插件: 根据配置，正在自动启动代理服务...")
            self.server_task = asyncio.create_task(self.start_local_proxy_server())

    # --- 流量格式化辅助函数 ---
    def _format_bytes(self, size: int) -> str:
        """将字节数格式化为可读的KB, MB, GB等"""
        if size < 1024:
            return f"{size} B"
        for unit in ['KB', 'MB', 'GB', 'TB', 'PB']: # 增加 PB 单位
            size /= 1024.0
            if size < 1024.0:
                return f"{size:.2f} {unit}"
        return f"{size:.2f} EB" # 额外增加 EB 单位

    # --- [新增] 流量解析辅助函数 ---
    def _parse_traffic_string(self, traffic_str: str) -> int | None:
        """解析流量字符串 (e.g., "5GB", "1000MB") 为字节数"""
        traffic_str = traffic_str.strip().upper()
        match = re.match(r'(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|PB)?', traffic_str)
        if not match:
            return None
        
        value = float(match.group(1))
        unit = match.group(2)
        
        if unit == "B" or unit is None:
            return int(value)
        elif unit == "KB":
            return int(value * 1024)
        elif unit == "MB":
            return int(value * 1024**2)
        elif unit == "GB":
            return int(value * 1024**3)
        elif unit == "TB":
            return int(value * 1024**4)
        elif unit == "PB":
            return int(value * 1024**5)
        return None

    # --- 数据持久化核心功能 ---
    async def _check_and_reset_daily_stats(self):
        """检查日期，如果跨天则重置每日统计"""
        today_str = date.today().isoformat()
        async with self.stats_lock:
            if self.stats.get("today_date") != today_str:
                logger.info(f"日期已更新，重置每日IP与流量统计。")
                # 记录前一天的流量到历史记录
                if self.stats.get("today_traffic_bytes") is not None:
                    # 初始化历史记录列表
                    if "daily_traffic_history" not in self.stats:
                        self.stats["daily_traffic_history"] = []
                    # 确保历史记录不超过最近3条
                    self.stats["daily_traffic_history"].append(self.stats["today_traffic_bytes"])
                    if len(self.stats["daily_traffic_history"]) > 3:
                        self.stats["daily_traffic_history"].pop(0) # 移除最旧的
                
                self.stats["today_date"] = today_str
                self.stats["today_ips_used"] = 0
                self.stats["today_requests_succeeded"] = 0
                self.stats["today_requests_failed"] = 0
                self.stats["today_traffic_bytes"] = 0
                await self._save_stats()

    def _load_stats_sync(self):
        """同步版本的数据加载，用于 __init__"""
        try:
            if self.stats_file.exists():
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    self.stats = json.load(f)
                logger.info("IP代理插件: 成功加载统计数据。")
            else:
                self.stats = {}
        except Exception as e:
            logger.error(f"IP代理插件: 加载统计数据失败: {e}，将使用默认值。")
            self.stats = {}

        self.stats.setdefault('total_ips_used', 0)
        self.stats.setdefault('today_date', '1970-01-01')
        self.stats.setdefault('today_ips_used', 0)
        self.stats.setdefault('today_requests_succeeded', 0)
        self.stats.setdefault('today_requests_failed', 0)
        self.stats.setdefault('total_traffic_bytes', 0)
        self.stats.setdefault('today_traffic_bytes', 0)
        self.stats.setdefault('daily_traffic_history', []) # 新增：每日流量历史记录
        self.stats.setdefault('total_traffic_limit_bytes', 0) # 新增：总流量限制

    async def _save_stats(self):
        """保存统计数据到文件，现在是异步的，并且受锁保护"""
        try:
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=4)
        except Exception as e:
            logger.error(f"IP代理插件: 保存统计数据失败: {e}")

    async def _increment_request_counter(self, success: bool):
        """记录请求成功或失败，现在是异步的"""
        await self._check_and_reset_daily_stats()
        async with self.stats_lock:
            if success:
                self.stats['today_requests_succeeded'] += 1
            else:
                self.stats['today_requests_failed'] += 1
            await self._save_stats()

    async def _increment_ip_usage_counter(self):
        """记录IP使用量，现在是异步的"""
        await self._check_and_reset_daily_stats()
        async with self.stats_lock:
            self.stats['total_ips_used'] += 1
            self.stats['today_ips_used'] += 1
            await self._save_stats()

    async def _forward_and_track(self, src: StreamReader, dst: StreamWriter):
        """
        从源读取数据，写入目标，并实时统计流量。
        """
        try:
            while not src.at_eof():
                data = await src.read(4096)
                if not data: break
                
                # --- 核心流量统计逻辑 ---
                traffic_this_chunk = len(data)
                async with self.stats_lock:
                    self.stats['total_traffic_bytes'] += traffic_this_chunk
                    self.stats['today_traffic_bytes'] += traffic_this_chunk
                    
                    # 检查总流量限制
                    total_limit = self.stats.get('total_traffic_limit_bytes', 0)
                    if total_limit > 0 and self.stats['total_traffic_bytes'] >= total_limit:
                        logger.warning(f"总流量已达到或超过限制 ({self._format_bytes(total_limit)})，将停止当前转发。")
                        # [修改 2] 仅保留 break，不再取消整个服务器任务
                        break 
                
                dst.write(data)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError): 
            pass
        finally:
            if not dst.is_closing():
                dst.close()
    
    # 重写 handle_connection，调用新的转发和统计方法
    async def handle_connection(self, reader: StreamReader, writer: StreamWriter):
        addr = writer.get_extra_info('peername')
        remote_writer: StreamWriter | None = None
        is_counted = False

        # 在处理新连接前检查流量限制，如果已达到限制，则拒绝连接
        async with self.stats_lock:
            total_limit = self.stats.get('total_traffic_limit_bytes', 0)
            if total_limit > 0 and self.stats['total_traffic_bytes'] >= total_limit:
                logger.warning(f"总流量已达到限制，拒绝新连接来自 {addr}。")
                writer.write(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n')
                await writer.drain()
                if not writer.is_closing(): writer.close()
                return

        try:
            initial_data = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            if not initial_data: return

            # ... (域名白名单验证逻辑不变) ...
            allowed_domains = set(self.config.get("allowed_domains", []))
            if not allowed_domains:
                # 如果没有配置白名单，则直接返回，不处理任何请求
                logger.warning(f"IP代理插件: 未配置allowed_domains，拒绝所有代理请求。")
                writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return

            hostname = self._extract_hostname(initial_data)
            if not hostname or hostname not in allowed_domains:
                logger.warning(f"IP代理插件: 拒绝来自 {addr} 的非白名单域名请求: {hostname if hostname else '未知主机'}")
                writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return
            
            logger.debug(f"接受来自 {addr} 的请求，转发到白名单主机: {hostname}")
            remote_ip, remote_port = await self.get_valid_ip()
            if not remote_ip or not remote_port:
                logger.error(f"无法为来自 {addr} 的白名单请求获取有效代理IP。")
                await self._increment_request_counter(success=False); is_counted = True
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return

            connect_timeout = self.config.get("connect_timeout", 10)
            conn_future = asyncio.open_connection(remote_ip, remote_port)
            remote_reader, remote_writer = await asyncio.wait_for(conn_future, timeout=connect_timeout)
            
            await self._increment_request_counter(success=True); is_counted = True

            # 将首个数据块的流量也计入统计
            traffic_initial_chunk = len(initial_data)
            async with self.stats_lock:
                self.stats['total_traffic_bytes'] += traffic_initial_chunk
                self.stats['today_traffic_bytes'] += traffic_initial_chunk
            
                # 再次检查总流量限制
                total_limit = self.stats.get('total_traffic_limit_bytes', 0)
                if total_limit > 0 and self.stats['total_traffic_bytes'] >= total_limit:
                    logger.warning(f"总流量已达到或超过限制 ({self._format_bytes(total_limit)})，关闭当前连接。")
                    writer.write(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n')
                    await writer.drain()
                    if not remote_writer.is_closing(): remote_writer.close()
                    if not writer.is_closing(): writer.close()
                    # 停止代理服务
                    if self.server_task and not self.server_task.done():
                        self.server_task.cancel()
                        self.server_task = None
                    return
                    
            remote_writer.write(initial_data)
            await remote_writer.drain()

            # 创建任务时调用新的 _forward_and_track 方法
            task1 = asyncio.create_task(self._forward_and_track(reader, remote_writer))
            task2 = asyncio.create_task(self._forward_and_track(remote_reader, writer))
            
            done, pending = await asyncio.wait([task1, task2], return_when=asyncio.FIRST_COMPLETED)
            for task in pending: task.cancel()

        except asyncio.TimeoutError:
            logger.debug(f"客户端 {addr} 在10秒内未发送有效请求头，连接关闭。")
            if not is_counted: await self._increment_request_counter(success=False)
        except Exception as e:
            logger.error(f"处理连接 {addr} 时发生错误: {e!r}")
            if not is_counted: await self._increment_request_counter(success=False)
        finally:
            if remote_writer and not remote_writer.is_closing(): remote_writer.close()
            if not writer.is_closing(): writer.close()
            # 在连接结束时最终保存一次统计数据，确保流量被记录
            async with self.stats_lock:
                await self._save_stats()
            logger.debug(f"与 {addr} 的连接已关闭。")

    # --- [修改] 代理状态指令，展示流量信息 ---
    @filter.command("代理状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        await self._check_and_reset_daily_stats() # 确保数据最新
        
        status_text = "✅运行中" if self.server_task and not self.server_task.done() else "❌已停止"
        ip_text = f"{self.current_ip}:{self.current_port}" if self.current_ip else "无"
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)

        # 加锁读取，防止在读取时数据被其他协程修改
        async with self.stats_lock:
            succeeded = self.stats.get('today_requests_succeeded', 0)
            failed = self.stats.get('today_requests_failed', 0)
            total_ips = self.stats.get('total_ips_used', 0)
            today_ips = self.stats.get('today_ips_used', 0)
            total_traffic = self.stats.get('total_traffic_bytes', 0)
            today_traffic = self.stats.get('today_traffic_bytes', 0)
            total_traffic_limit = self.stats.get('total_traffic_limit_bytes', 0)
            daily_traffic_history = self.stats.get('daily_traffic_history', [])

        total_reqs = succeeded + failed
        success_rate_text = f"{(succeeded / total_reqs * 100):.2f}%" if total_reqs > 0 else "N/A"
        
        # 计算剩余流量
        remaining_traffic = "无限制"
        if total_traffic_limit > 0:
            remaining_bytes = total_traffic_limit - total_traffic
            remaining_traffic = self._format_bytes(max(0, remaining_bytes))

        # 计算预计可用天数
        estimated_days = "N/A"
        avg_daily_traffic = 0
        if len(daily_traffic_history) > 0:
            avg_daily_traffic = sum(daily_traffic_history) / len(daily_traffic_history)

        if total_traffic_limit > 0 and avg_daily_traffic > 0:
            estimated_days = f"{(remaining_bytes / avg_daily_traffic):.2f} 天"


        status_message = (
            f"--- IP代理插件状态 ---\n"
            f"运行状态: {status_text}\n"
            f"监听地址: {listen_host}:{local_port}\n"
            f"当前代理IP: {ip_text}\n"
            f"--------------------\n"
            f"总流量限制: {self._format_bytes(total_traffic_limit) if total_traffic_limit > 0 else '无限制'}\n" # 新增
            f"总使用流量: {self._format_bytes(total_traffic)}\n"
            f"剩余流量: {remaining_traffic}\n" # 新增
            f"今日使用流量: {self._format_bytes(today_traffic)}\n"
            f"每日平均流量 (最近{len(daily_traffic_history)}天): {self._format_bytes(avg_daily_traffic) if len(daily_traffic_history) > 0 else 'N/A'}\n" # 新增
            f"预计可用天数: {estimated_days}\n" # 新增
            f"今日请求成功率: {success_rate_text} ({succeeded}/{total_reqs})\n"
            f"IP总使用量: {total_ips}\n"
            f"今日IP使用量: {today_ips}\n"
            f"--------------------\n"
            f"白名单域名: {', '.join(self.config.get('allowed_domains', ['未配置']))}"
        )
        return event.plain_result(status_message)
        
    async def get_new_ip(self) -> tuple[str | None, int | None]:
        api_url = self.config.get("api_url")
        if not api_url or "YOUR_TOKEN" in api_url:
            logger.warning("IP代理插件: API URL 未配置，无法获取新IP。")
            return None, None
        try:
            if self.http_session.closed: self.http_session = aiohttp.ClientSession()
            async with self.http_session.get(api_url) as response:
                response.raise_for_status()
                ip_port = (await response.text()).strip()
                if ":" in ip_port:
                    ip, port_str = ip_port.split(":")
                    port = int(port_str)
                    await self._increment_ip_usage_counter()
                    async with self.stats_lock:
                        logger.info(f"IP代理插件: 获取到新IP: {ip}:{port}。今日已使用: {self.stats['today_ips_used']}个, 总计: {self.stats['total_ips_used']}个")
                    return ip, port
                else:
                    logger.warning(f"IP代理插件: API返回格式错误: {ip_port}")
                    return None, None
        except Exception as e:
            logger.error(f"IP代理插件: 获取IP失败: {e}")
            return None, None
            
    @filter.command("开启代理", alias={"启动代理", "代理开启"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        if self.server_task and not self.server_task.done():
            return event.plain_result("代理服务已经在运行中。")
        self.server_task = asyncio.create_task(self.start_local_proxy_server())
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        return event.plain_result(f"代理服务已启动，监听于 {listen_host}:{local_port}")

    @filter.command("关闭代理", alias={"代理关闭", "取消代理"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def stop_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self.server_task or self.server_task.done():
            return event.plain_result("代理服务未在运行。")
        self.server_task.cancel()
        try: await self.server_task
        except asyncio.CancelledError: pass
        self.server_task = None; self.server = None
        return event.plain_result("代理服务已停止。")

    @filter.command("切换IP")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_ip(self, event: AstrMessageEvent):
        yield event.plain_result("正在强制切换代理IP...")
        
        async with self.ip_lock:
            self.current_ip = None
            self.current_port = None
            logger.info("管理员指令: 强制切换IP，当前IP已失效。")

        new_ip, new_port = await self.get_valid_ip()
        
        if new_ip and new_port:
            yield event.plain_result(f"✅ IP切换成功！\n新代理IP: {new_ip}:{new_port}")
        else:
            yield event.plain_result("❌ IP切换失败！无法获取到有效的代理IP，请检查API或网络。")

    def _extract_hostname(self, request_data: bytes) -> str | None:
        try:
            request_str = request_data.decode('utf-8', errors='ignore')
            connect_match = re.search(r'CONNECT\s+([a-zA-Z0-9.-]+):\d+', request_str, re.IGNORECASE)
            if connect_match: return connect_match.group(1).lower()
            host_match = re.search(r'Host:\s+([a-zA-Z0-9.-]+)', request_str, re.IGNORECASE)
            if host_match: return host_match.group(1).lower()
        except Exception: pass
        return None

    async def is_ip_valid(self, ip: str, port: int) -> bool:
        validation_url = self.config.get("validation_url", "http://www.baidu.com")
        timeout_config = aiohttp.ClientTimeout(total=self.config.get("validation_timeout", 5))
        proxy_url = f"http://{ip}:{port}"
        try:
            if self.http_session.closed: self.http_session = aiohttp.ClientSession()
            async with self.http_session.get(validation_url, proxy=proxy_url, timeout=timeout_config) as response:
                if response.status == 200:
                    logger.info(f"IP {ip}:{port} 验证成功。")
                    return True
        except Exception as e:
            logger.warning(f"IP {ip}:{port} 访问 {validation_url} 验证失败: {e}")
        return False

    async def get_valid_ip(self) -> tuple[str | None, int | None]:
        async with self.ip_lock:
            ip_expiration_time = self.config.get("ip_expiration_time", 300)
            validation_interval = self.config.get("validation_interval", 60)
            if self.current_ip and self.current_port and self.last_validation_time:
                ip_age = time.time() - self.last_validation_time
                if ip_expiration_time > 0 and ip_age > ip_expiration_time:
                    logger.info(f"IP {self.current_ip}:{self.current_port} 已使用超过 {ip_expiration_time} 秒，强制获取新IP。")
                    self.current_ip = None
                    self.current_port = None
                elif ip_age < validation_interval:
                    logger.debug(f"使用缓存中的IP: {self.current_ip}:{self.current_port} (验证间隔内)")
                    return self.current_ip, self.current_port
                else:
                    logger.debug(f"IP {self.current_ip}:{self.current_port} 需重新验证...")
                    if await self.is_ip_valid(self.current_ip, self.current_port):
                        self.last_validation_time = time.time()
                        logger.debug(f"IP {self.current_ip}:{self.current_port} 验证成功，继续使用。")
                        return self.current_ip, self.current_port
                    else:
                        logger.info(f"IP {self.current_ip}:{self.current_port} 重新验证失败，获取新IP。")
                        self.current_ip = None
            if not self.current_ip:
                for _ in range(3):
                    new_ip, new_port = await self.get_new_ip()
                    if new_ip and new_port:
                        if await self.is_ip_valid(new_ip, new_port):
                            self.current_ip, self.current_port = new_ip, new_port
                            self.last_validation_time = time.time()
                            return self.current_ip, self.current_port
                    logger.warning("获取的新IP无效或验证失败，1秒后重试...")
                    await asyncio.sleep(1)
            if not self.current_ip:
                logger.error("多次尝试后，仍无法获取到有效的IP地址。")
                return None, None
            return self.current_ip, self.current_port

    async def start_local_proxy_server(self):
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        try:
            self.server = await asyncio.start_server(self.handle_connection, listen_host, local_port)
            logger.info(f"本地代理服务器已启动，监听地址: {listen_host}:{local_port}")
            await self.server.serve_forever()
        except asyncio.CancelledError:
            logger.info("本地代理服务器任务被取消。")
        except Exception as e:
            logger.error(f"启动本地代理服务器失败: {e}，请检查端口是否被占用或配置是否正确。")
        finally:
            if self.server and self.server.is_serving():
                self.server.close(); await self.server.wait_closed()
            logger.info("本地代理服务器已关闭。")
            self.server = None; self.server_task = None

    async def terminate(self):
        logger.info("IP代理插件正在终止...")
        if self.server_task and not self.server_task.done():
            self.server_task.cancel()
            try: await self.server_task
            except asyncio.CancelledError: pass
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        logger.info("IP代理插件已终止。")
    
    # --- 其他修改配置的指令 ---
    @filter.command("修改代理API")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_api_url(self, event: AstrMessageEvent, api_url: str) -> MessageEventResult:
        self.config["api_url"] = api_url
        self.config.save_config()
        return event.plain_result(f"✅ 代理API地址已更新为: {api_url}")

    @filter.command("修改监听地址")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_host(self, event: AstrMessageEvent, host: str) -> MessageEventResult:
        self.config["listen_host"] = host
        self.config.save_config()
        return event.plain_result(f"✅ 监听地址已更新为: {host}\n重启代理后生效。")

    @filter.command("修改监听端口")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_port(self, event: AstrMessageEvent, port: int) -> MessageEventResult:
        self.config["local_port"] = port
        self.config.save_config()
        return event.plain_result(f"✅ 监听端口已更新为: {port}\n重启代理后生效。")

    @filter.command("修改测试url")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_validation_url(self, event: AstrMessageEvent, url: str) -> MessageEventResult:
        self.config["validation_url"] = url
        self.config.save_config()
        return event.plain_result(f"✅ 验证URL已更新为: {url}")
        
    @filter.command("修改IP失效时间")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_ip_expiration_time(self, event: AstrMessageEvent, seconds: int) -> MessageEventResult:
        self.config["ip_expiration_time"] = seconds
        self.config.save_config()
        if seconds > 0:
            return event.plain_result(f"✅ IP绝对失效时间已更新为: {seconds} 秒。")
        else:
            return event.plain_result(f"✅ IP绝对失效时间已设置为永不强制失效。")

    # --- [新增] 设置总流量限制命令 ---
    @filter.command("设置总流量")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_total_traffic_limit(self, event: AstrMessageEvent, traffic_size: str) -> MessageEventResult:
        parsed_bytes = self._parse_traffic_string(traffic_size)
        if parsed_bytes is None:
            return event.plain_result(f"❌ 无效的流量大小格式。请使用例如: 5GB, 1000MB, 2TB。")
        
        async with self.stats_lock:
            self.stats['total_traffic_limit_bytes'] = parsed_bytes
            await self._save_stats()
        
        return event.plain_result(f"✅ 总流量限制已设定为: {self._format_bytes(parsed_bytes)}")

    # --- [新增] 设置已使用流量命令 ---
    @filter.command("设置已使用流量")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_used_traffic(self, event: AstrMessageEvent, traffic_size: str) -> MessageEventResult:
        parsed_bytes = self._parse_traffic_string(traffic_size)
        if parsed_bytes is None:
            return event.plain_result(f"❌ 无效的流量大小格式。请使用例如: 5GB, 1000MB, 2TB。")
        
        async with self.stats_lock:
            self.stats['total_traffic_bytes'] = parsed_bytes
            await self._save_stats()
        
        return event.plain_result(f"✅ 已使用总流量已更新为: {self._format_bytes(parsed_bytes)}")