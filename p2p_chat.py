#!/usr/bin/env python3

import argparse
import json
import socket
import struct
import threading
import time
import uuid
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Типы сообщений TCP
MSG_CHAT = 1
MSG_HELLO = 2
# Формат сообщений TCP
FRAME_HEADER = struct.Struct("!BI")  # type (1 byte), length (4 bytes)

# класс пира для хранения информации о пользователе
class Peer:
    def __init__(self, sock: socket.socket, addr: Tuple[str, int]):
        self.sock = sock
        self.addr = addr
        self.node_id: Optional[str] = None
        self.name: Optional[str] = None
        self.lock = threading.Lock()


# класс узла для работы с UDP и TCP
class P2PChatNode:
    def __init__(self, bind_ip: str, udp_port: int, tcp_port: int, name: str):
        self.bind_ip = bind_ip
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.name = name
        self.node_id = f"{name}-{uuid.uuid4().hex[:8]}"
        # Для локального теста на одном ПК (127.0.0.x) используем multicast,
        # т.к. SO_REUSEPORT обычно распределяет UDP-пакеты между сокетами, а не дублирует.
        self.loopback_multicast_group = "239.255.0.1"

        # событие для остановки узла
        self.running = threading.Event()
        self.running.set()

        # сокеты для UDP и TCP
        self.udp_sock: Optional[socket.socket] = None
        self.tcp_server: Optional[socket.socket] = None

        # словари для хранения пиров по id и сокету
        self.peers_by_id: Dict[str, Peer] = {}
        self.peers_by_sock: Dict[socket.socket, Peer] = {}
        self.peers_lock = threading.Lock()

        # список для хранения истории событий
        self.history: List[str] = []
        self.history_lock = threading.Lock()

        # чтобы не спамить "Обнаружен узел" на discovery heartbeat
        self._discover_log_ts_by_id: Dict[str, float] = {}
        self._discover_log_interval_s = 15.0

    # метод для получения текущего времени
    @staticmethod
    def now() -> str:
        return datetime.now().strftime("%H:%M:%S")
    # метод для логирования событий
    # добавляет время и текст события в историю и выводит в консоль
    def log_event(self, text: str) -> None:
        line = f"[{self.now()}] {text}"
        with self.history_lock:
            self.history.append(line)
        print(line)

    # метод для запуска узла
    # запускает UDP и TCP сервера и создает поток для приема UDP сообщений
    def start(self) -> None:
        self.start_udp_listener()
        self.start_tcp_server()
# создает поток для приема UDP сообщений
        threading.Thread(target=self.udp_discovery_loop, daemon=True).start()
        # периодически повторяем discovery, чтобы узлы точно обнаруживали друг друга
        threading.Thread(target=self.discovery_heartbeat_loop, daemon=True).start()

        # Первичное объявление о себе
        self.send_discovery_broadcast()
        self.log_event(
            f"Узел запущен: name={self.name}, id={self.node_id}, ip={self.bind_ip}, udp={self.udp_port}, tcp={self.tcp_port}"
        )

    # периодическая рассылка discovery (на случай потерь/разного порядка запуска)
    def discovery_heartbeat_loop(self) -> None:
        while self.running.is_set():
            self.send_discovery_broadcast()
            # интервал небольшой для локального теста, но не спамим слишком часто
            for _ in range(3):
                if not self.running.is_set():
                    return
                time.sleep(1)
# метод для остановки узла
# останавливает UDP и TCP сервера и закрывает все соединения
    def stop(self) -> None:
        self.running.clear()
        self.send_leave_broadcast()
# закрывает все соединения с пирами
        with self.peers_lock:
            peers = list(self.peers_by_sock.values())
        for peer in peers:
            self.close_peer(peer)
# закрывает сокеты UDP и TCP
        if self.udp_sock is not None:
            try:
                self.udp_sock.close()
            except OSError:
                pass
        if self.tcp_server is not None:
            try:
                self.tcp_server.close()
            except OSError:
                pass
# метод для запуска UDP сервера
# создает сокет UDP и привязывает его к адресу и порту
    def start_udp_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # устанавливает опции для сокета
        # позволяет повторно использовать адрес и порт
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # позволяет нескольким процессам слушать один UDP порт (нужно для теста 127.0.0.x на одном ПК)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        # включает broadcast (для обычной сети); для loopback-теста будем использовать multicast
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # привязывает сокет к адресу и порту
        try:
            # Для discovery слушаем на всех интерфейсах (так проще для broadcast/multicast)
            sock.bind(("0.0.0.0", self.udp_port))
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось открыть UDP 0.0.0.0:{self.udp_port}. Порт занят или адрес недоступен: {exc}"
            )
        sock.settimeout(1.0)
        self.udp_sock = sock

        # Если тестируем несколько узлов на одном Mac через 127.0.0.x — включаем multicast на lo0.
        if self.bind_ip.startswith("127."):
            try:
                # Выбираем интерфейс loopback для multicast-исходящих
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                mreq = socket.inet_aton(self.loopback_multicast_group) + socket.inet_aton("127.0.0.1")
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                # если multicast недоступен, дальше можно попробовать broadcast как fallback
                pass
# метод для запуска TCP сервера
# создает сокет TCP и привязывает его к адресу и порту
    def start_tcp_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # устанавливает опции для сокета
        # позволяет повторно использовать адрес и порт
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # позволяет принимать соединения на все сетевые интерфейсы
        try:
            server.bind((self.bind_ip, self.tcp_port))
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось открыть TCP {self.bind_ip}:{self.tcp_port}. Порт занят или адрес недоступен: {exc}"
            )
        server.listen(16)
        server.settimeout(1.0)
        self.tcp_server = server
# создает поток для приема входящих TCP соединений
        threading.Thread(target=self.accept_loop, daemon=True).start()

    # метод для отправки UDP broadcast сообщения
    # отправляет сообщение о себе всем узлам в сети
    def send_discovery_broadcast(self) -> None:
        # проверяет, что сокет UDP открыт
        if self.udp_sock is None:
            return
        # создает payload для сообщения
        payload = {
            # тип сообщения - discover
            # discover - обнаружение узлов в сети
            "type": "discover",
            # идентификатор узла
            "node_id": self.node_id,
            # имя узла
            "name": self.name,
            # ip узла (важно для loopback-теста, где recvfrom может вернуть 127.0.0.1 для всех)
            "ip": self.bind_ip,
            # порт TCP
            "tcp_port": self.tcp_port,
        }
        # преобразует payload в JSON и кодирует в байты
        data = json.dumps(payload).encode("utf-8")
        # отправляет сообщение всем узлам в сети
        # широковещательный адрес 255.255.255.255
        # порт UDP
        try:
            if self.bind_ip.startswith("127."):
                self.udp_sock.sendto(data, (self.loopback_multicast_group, self.udp_port))
            else:
                bcast = "255.255.255.255"
                self.udp_sock.sendto(data, (bcast, self.udp_port))
        except OSError:
            pass

    # метод для отправки UDP broadcast сообщения
    # отправляет сообщение о выходе из сети всем узлам в сети
    def send_leave_broadcast(self) -> None:
        # проверяет, что сокет UDP открыт
        if self.udp_sock is None:
            return
        # создает payload для сообщения
        payload = {
            "type": "leave",
            "node_id": self.node_id,
            "name": self.name,
        }
        # преобразует payload в JSON и кодирует в байты
        data = json.dumps(payload).encode("utf-8")
        # отправляет сообщение всем узлам в сети
        # широковещательный адрес 255.255.255.255
        # порт UDP
        try:
            if self.bind_ip.startswith("127."):
                self.udp_sock.sendto(data, (self.loopback_multicast_group, self.udp_port))
            else:
                bcast = "255.255.255.255"
                self.udp_sock.sendto(data, (bcast, self.udp_port))
        except OSError:
            pass

    # метод для приема UDP сообщений
    # принимает UDP сообщения и обрабатывает их
    def udp_discovery_loop(self) -> None:
        # проверяет, что сокет UDP открыт
        assert self.udp_sock is not None
        # цикл для приема UDP сообщений
        while self.running.is_set():
            try:
                # получает данные и адрес отправителя
                data, addr = self.udp_sock.recvfrom(65535)
            # если сокет заблокирован, пропускаем
            except socket.timeout:
                continue
            except OSError:
                break
# пытается декодировать данные в JSON
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
# получает тип сообщения и идентификатор узла
            mtype = payload.get("type")
            node_id = payload.get("node_id")
            # если идентификатор узла не получен или равен идентификатору текущего узла, пропускаем
            if not node_id or node_id == self.node_id:
                continue
# если тип сообщения - discover, соединяемся с узлом
            if mtype == "discover":
                # получаем имя и порт узла
                peer_name = payload.get("name", "unknown")
                # получаем порт узла
                peer_tcp = payload.get("tcp_port")
                if not isinstance(peer_tcp, int):
                    continue
# определяем ip узла (если передали в payload — используем его)
                peer_ip = payload.get("ip")
                if not isinstance(peer_ip, str) or not peer_ip:
                    peer_ip = addr[0]
                # если узел уже подключён — не спамим логом и не коннектимся повторно
                with self.peers_lock:
                    already_connected = node_id in self.peers_by_id
                if already_connected:
                    continue

                # логируем discovery с rate-limit (на случай повторов до TCP)
                now_ts = time.time()
                last_ts = self._discover_log_ts_by_id.get(node_id, 0.0)
                if now_ts - last_ts >= self._discover_log_interval_s:
                    self._discover_log_ts_by_id[node_id] = now_ts
                    self.log_event(f"Обнаружен узел: {peer_name} ({peer_ip}:{peer_tcp})")
                self.connect_to_peer(peer_ip, peer_tcp, peer_id=node_id)
            elif mtype == "leave":
                peer_name = payload.get("name", "unknown")
                peer_ip = payload.get("ip")
                if not isinstance(peer_ip, str) or not peer_ip:
                    peer_ip = addr[0]
                self.log_event(f"Узел отключился: {peer_name} ({peer_ip})")
                peer_id = payload.get("node_id")
                if isinstance(peer_id, str):
                    with self.peers_lock:
                        p = self.peers_by_id.get(peer_id)
                    if p is not None:
                        self.close_peer(p)
# метод для приема входящих TCP соединений
# принимает TCP соединения и обрабатывает их
    def accept_loop(self) -> None:
        assert self.tcp_server is not None
        # цикл для приема входящих TCP соединений
        while self.running.is_set():
            # пытается принять соединение
            try:
                sock, addr = self.tcp_server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
# создает пир для нового соединения
            peer = Peer(sock, addr)
            # добавляет пир в словарь пиров по сокету
            with self.peers_lock:
                self.peers_by_sock[sock] = peer
            # создает поток для чтения данных от пира
            threading.Thread(target=self.peer_reader_loop, args=(peer,), daemon=True).start()

            # Сразу отправляем hello с нашей идентификацией
            # создает payload для сообщения hello
            hello = {
                # идентификатор узла
                "node_id": self.node_id,
                "name": self.name,
                "ip": self.bind_ip,
                "tcp_port": self.tcp_port,
            }
            # отправляет сообщение hello пиру
            self.send_frame(peer, MSG_HELLO, hello)

    # метод для соединения с пиром
    # соединяется с пиром по IP и порту
    def connect_to_peer(self, ip: str, port: int, peer_id: Optional[str] = None) -> None:
        # проверяет, что IP и порт не совпадают с текущим узлом
        # Защита от соединения с собой
        if ip == self.bind_ip and port == self.tcp_port:
            return

        # Если уже знаем peer_id и он подключен — второй раз не коннектимся
        with self.peers_lock:
            if peer_id and peer_id in self.peers_by_id:
                return
            # Если уже есть подключение к этому ip:port, второй раз не коннектимся
            for p in self.peers_by_sock.values():
                if p.addr[0] == ip and p.addr[1] == port:
                    return
        # создает сокет TCP
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # устанавливает таймаут для сокета
        sock.settimeout(3.0)
        # пытается соединиться с пиром
        try:
            sock.connect((ip, port))
        except OSError as exc:
            sock.close()
            # без спама: лог только если discovery дал peer_id (то есть это реальная попытка подключения)
            if peer_id:
                self.log_event(f"TCP connect не удался: {ip}:{port} ({exc})")
            return
        sock.settimeout(None)
        # создает пир для нового соединения
        peer = Peer(sock, (ip, port))
        # добавляет пир в словарь пиров по сокету
        with self.peers_lock:
            self.peers_by_sock[sock] = peer
        # создает поток для чтения данных от пира
        threading.Thread(target=self.peer_reader_loop, args=(peer,), daemon=True).start()
        # создает payload для сообщения hello
        hello = {
            "node_id": self.node_id,
            "name": self.name,
            "ip": self.bind_ip,
            "tcp_port": self.tcp_port,
        }
        self.send_frame(peer, MSG_HELLO, hello)
    # метод для чтения данных от пира
    # читает данные от пира и обрабатывает их
    def peer_reader_loop(self, peer: Peer) -> None:
        sock = peer.sock
        try:
            while self.running.is_set():
                frame = self.recv_frame(sock)
                if frame is None:
                    break
                mtype, payload = frame

                if mtype == MSG_HELLO:
                    self.handle_hello(peer, payload)
                elif mtype == MSG_CHAT:
                    self.handle_chat(peer, payload)
        finally:
            self.close_peer(peer)

    # метод для обработки hello сообщения
    # обрабатывает hello сообщение и устанавливает соединение с пиром
    def handle_hello(self, peer: Peer, payload: dict) -> None:
        # получает идентификатор и имя узла
        node_id = payload.get("node_id")
        name = payload.get("name")
        if not node_id or not name:
            return
        # peer может подключиться с ephemeral-port (если это входящее соединение),
        # поэтому заменяем addr на объявленные ip:tcp_port из hello (если они валидны).
        advertised_ip = payload.get("ip")
        advertised_tcp = payload.get("tcp_port")
        if isinstance(advertised_ip, str) and advertised_ip and isinstance(advertised_tcp, int):
            peer.addr = (advertised_ip, advertised_tcp)
        # проверяет, что этот node_id уже подключен, если да, то закрывает соединение с текущим пиром  
        with self.peers_lock:
            # Если этот node_id уже подключён, оставляем только одно соединение
            existing = self.peers_by_id.get(node_id)
            if existing is not None and existing is not peer:
                # детерминированное правило: оставляем соединение с меньшим fileno
                # fileno - файловый дескриптор сокета
                if existing.sock.fileno() <= peer.sock.fileno():
                    self.close_peer(peer)
                    return
                self.close_peer(existing)

            peer.node_id = node_id
            peer.name = name
            self.peers_by_id[node_id] = peer

        self.log_event(f"TCP-соединение установлено: {name} ({peer.addr[0]}:{peer.addr[1]})")

    def handle_chat(self, peer: Peer, payload: dict) -> None:
        sender_id = payload.get("sender_id")
        sender_name = payload.get("sender_name", "unknown")
        sender_ip = payload.get("sender_ip", peer.addr[0])
        text = payload.get("text", "")

        if not sender_id:
            return

        self.log_event(f"{sender_name} ({sender_ip}): {text}")

    def send_chat(self, text: str) -> None:
        payload = {
            "sender_id": self.node_id,
            "sender_name": self.name,
            "sender_ip": self.bind_ip,
            "text": text,
            "ts": int(time.time()),
        }

        self.log_event(f"Я: {text}")
        self.broadcast_chat_payload(payload)

    # метод для отправки чата всем пирам
    # отправляет чат всем пирам
    def broadcast_chat_payload(self, payload: dict) -> None:
        # получает список пиров
        with self.peers_lock:
            peers = list(self.peers_by_sock.values())
        for peer in peers:
            self.send_frame(peer, MSG_CHAT, payload)

    # метод для отправки фрейма
    # отправляет фрейм пиру
    def send_frame(self, peer: Peer, mtype: int, payload: dict) -> None:
        # преобразует payload в JSON и кодирует в байты
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        packet = FRAME_HEADER.pack(mtype, len(data)) + data
        # отправляет фрейм пиру
        try:
            with peer.lock:
                peer.sock.sendall(packet)
        except OSError:
            self.close_peer(peer)

    # метод для чтения данных от сокета
    # читает данные от сокета и обрабатывает их
    def recv_exact(self, sock: socket.socket, size: int) -> Optional[bytes]:
        # создает список для хранения данных
        chunks = []
        remaining = size
        # цикл для чтения данных от сокета
        while remaining > 0:
            try:
                chunk = sock.recv(remaining)
            # если сокет заблокирован, пропускаем
            except OSError:
                return None
            # если данных нет, пропускаем
            if not chunk:
                # возвращает None
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    # метод для чтения фрейма
    # читает фрейм от сокета и обрабатывает их  
    def recv_frame(self, sock: socket.socket) -> Optional[Tuple[int, dict]]:
        # читает заголовок фрейма
        header = self.recv_exact(sock, FRAME_HEADER.size)
        # если заголовок не получен, пропускаем
        if header is None:
            return None
        # распаковывает заголовок фрейма
        mtype, length = FRAME_HEADER.unpack(header)
        # если длина фрейма некорректна, пропускаем
        if length < 0 or length > 10_000_000:
            return None
        # читает payload фрейма
        payload_raw = self.recv_exact(sock, length)
        # если payload не получен, пропускаем
        if payload_raw is None:
            return None
        # пытается декодировать payload в JSON
        try:
            payload = json.loads(payload_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return mtype, payload

    # метод для закрытия соединения с пиром
    # закрывает соединение с пиром и удаляет его из списка пиров
    def close_peer(self, peer: Peer) -> None:
        # удаляет пир из словаря пиров по сокету
        with self.peers_lock:
            # удаляет пир из словаря пиров по сокету
            self.peers_by_sock.pop(peer.sock, None)
            if peer.node_id and self.peers_by_id.get(peer.node_id) is peer:
                self.peers_by_id.pop(peer.node_id, None)
        # пытается закрыть сокет пира
        try:
            peer.sock.close()
        except OSError:
            pass
        # если имя пира не пустое, логируем закрытие соединения
        if peer.name:
            self.log_event(f"TCP-соединение закрыто: {peer.name} ({peer.addr[0]}:{peer.addr[1]})")

    # метод для вывода списка пиров
    # выводит список пиров
    def print_peers(self) -> None:
        # получает список пиров
        with self.peers_lock:
            peers = list(self.peers_by_id.values())
        # если пиров нет, выводим сообщение
        if not peers:
            print("Нет активных TCP-соединений")
            return
        # выводим список пиров
        print("Активные узлы:")
        for p in peers:
            print(f"- {p.name} ({p.addr[0]}:{p.addr[1]}) id={p.node_id}")

    # метод для вывода истории событий
    # выводит историю событий
    def print_history(self) -> None:
        # получает список событий
        with self.history_lock:
            items = list(self.history)
        # если событий нет, выводим сообщение
        if not items:
            print("История пуста")
            return
        # выводим список событий
        print("История событий:")
        for line in items:
            print(line)


# метод для парсинга аргументов
# парсит аргументы командной строки
def parse_args() -> argparse.Namespace:
    # создает парсер аргументов
    parser = argparse.ArgumentParser(description="P2P чат (UDP discovery + TCP сообщения)")
    # help - описание аргумента
    parser.add_argument("--name", help="Имя пользователя")
    parser.add_argument("--bind-ip", help="IP для bind (например, 127.0.0.1, 127.0.0.2)")
    parser.add_argument("--udp-port", type=int, help="UDP порт discovery")
    parser.add_argument("--tcp-port", type=int, help="TCP порт чата")
    parser.add_argument(
        "--no-stdin",
        action="store_true",
        help="Не читать stdin (полезно для запуска в фоне/автотестов)",
    )
    return parser.parse_args()


# метод для запроса аргументов, если они не переданы
# запрашивает аргументы у пользователя
def ask_if_missing(args: argparse.Namespace) -> argparse.Namespace:
    # если имя не передано, запрашиваем у пользователя
    if not args.name:
        args.name = input("Введите имя: ").strip() or "anon"
    # если bind IP не передан, запрашиваем у пользователя
    if not args.bind_ip:
        args.bind_ip = input("Введите bind IP (например, 127.0.0.1): ").strip() or "127.0.0.1"
    # если UDP порт не передан, запрашиваем у пользователя
    if not args.udp_port:
        args.udp_port = int(input("Введите UDP порт discovery (например, 50000): ").strip() or "50000")
    if not args.tcp_port:
        args.tcp_port = int(input("Введите TCP порт чата (например, 50001): ").strip() or "50001")
    return args


# метод для вывода справки
# выводит справку
def print_help() -> None:
    # выводим справку
    print("Команды:")
    print("  /help                    - показать справку")
    print("  /peers                   - показать активные соединения")
    print("  /history                 - показать историю событий")
    print("  /quit                    - выйти")
    print("  <текст>                  - отправить сообщение всем узлам")

# метод для запуска программы
# запускает программу
def main() -> None:
    args = ask_if_missing(parse_args())
    # создаем узел
    node = P2PChatNode(args.bind_ip, args.udp_port, args.tcp_port, args.name)
    try:
        # запускаем узел
        node.start()
    except RuntimeError as exc:
        print(f"Ошибка запуска: {exc}")
        return
    # выводим справку
    print_help()
    # цикл для ввода команд (или фоновый режим без stdin)
    try:
        if args.no_stdin or not sys.stdin.isatty():
            while True:
                time.sleep(1)
        else:
            while True:
                try:
                    line = input().strip()
                except EOFError:
                    break
                except KeyboardInterrupt:
                    break

                if not line:
                    continue
                if line == "/quit":
                    break
                if line == "/help":
                    print_help()
                    continue
                if line == "/peers":
                    node.print_peers()
                    continue
                if line == "/history":
                    node.print_history()
                    continue

                node.send_chat(line)
    finally:
        node.stop()


if __name__ == "__main__":
    main()
