#!/usr/bin/env python3
"""
P2P чат для лабораторной работы 3.

Требования, которые покрывает:
- одноранговый режим (каждый узел и сервер, и клиент);
- обнаружение узлов через UDP broadcast;
- обмен сообщениями через TCP;
- IP и порты задаются пользователем (не хардкодятся);
- проверка доступности портов при bind;
- UI не блокируется сетевыми операциями (сеть работает в фоновых потоках).
"""

import argparse
import json
import socket
import struct
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Типы фреймов TCP
MSG_CHAT = 1
MSG_HELLO = 2

FRAME_HEADER = struct.Struct("!BI")  # type (1 byte), length (4 bytes)


class Peer:
    def __init__(self, sock: socket.socket, addr: Tuple[str, int]):
        self.sock = sock
        self.addr = addr
        self.node_id: Optional[str] = None
        self.name: Optional[str] = None
        self.lock = threading.Lock()


class P2PChatNode:
    def __init__(self, bind_ip: str, udp_port: int, tcp_port: int, name: str):
        self.bind_ip = bind_ip
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.name = name
        self.node_id = f"{name}-{uuid.uuid4().hex[:8]}"

        self.running = threading.Event()
        self.running.set()

        self.udp_sock: Optional[socket.socket] = None
        self.tcp_server: Optional[socket.socket] = None

        self.peers_by_id: Dict[str, Peer] = {}
        self.peers_by_sock: Dict[socket.socket, Peer] = {}
        self.peers_lock = threading.Lock()

        self.history: List[str] = []
        self.history_lock = threading.Lock()

    @staticmethod
    def now() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def log_event(self, text: str) -> None:
        line = f"[{self.now()}] {text}"
        with self.history_lock:
            self.history.append(line)
        print(line)

    def start(self) -> None:
        self.start_udp_listener()
        self.start_tcp_server()

        threading.Thread(target=self.udp_discovery_loop, daemon=True).start()

        # Первичное объявление о себе
        self.send_discovery_broadcast()
        self.log_event(
            f"Узел запущен: name={self.name}, id={self.node_id}, ip={self.bind_ip}, udp={self.udp_port}, tcp={self.tcp_port}"
        )

    def stop(self) -> None:
        self.running.clear()
        self.send_leave_broadcast()

        with self.peers_lock:
            peers = list(self.peers_by_sock.values())
        for peer in peers:
            self.close_peer(peer)

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

    def start_udp_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind((self.bind_ip, self.udp_port))
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось открыть UDP {self.bind_ip}:{self.udp_port}. Порт занят или адрес недоступен: {exc}"
            )
        sock.settimeout(1.0)
        self.udp_sock = sock

    def start_tcp_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.bind_ip, self.tcp_port))
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось открыть TCP {self.bind_ip}:{self.tcp_port}. Порт занят или адрес недоступен: {exc}"
            )
        server.listen(16)
        server.settimeout(1.0)
        self.tcp_server = server

        threading.Thread(target=self.accept_loop, daemon=True).start()

    def send_discovery_broadcast(self) -> None:
        if self.udp_sock is None:
            return
        payload = {
            "type": "discover",
            "node_id": self.node_id,
            "name": self.name,
            "tcp_port": self.tcp_port,
        }
        data = json.dumps(payload).encode("utf-8")
        try:
            self.udp_sock.sendto(data, ("255.255.255.255", self.udp_port))
        except OSError:
            pass

    def send_leave_broadcast(self) -> None:
        if self.udp_sock is None:
            return
        payload = {
            "type": "leave",
            "node_id": self.node_id,
            "name": self.name,
        }
        data = json.dumps(payload).encode("utf-8")
        try:
            self.udp_sock.sendto(data, ("255.255.255.255", self.udp_port))
        except OSError:
            pass

    def udp_discovery_loop(self) -> None:
        assert self.udp_sock is not None
        while self.running.is_set():
            try:
                data, addr = self.udp_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            mtype = payload.get("type")
            node_id = payload.get("node_id")
            if not node_id or node_id == self.node_id:
                continue

            if mtype == "discover":
                peer_name = payload.get("name", "unknown")
                peer_tcp = payload.get("tcp_port")
                if not isinstance(peer_tcp, int):
                    continue

                self.log_event(f"Обнаружен узел: {peer_name} ({addr[0]}:{peer_tcp})")
                self.connect_to_peer(addr[0], peer_tcp)
            elif mtype == "leave":
                peer_name = payload.get("name", "unknown")
                self.log_event(f"Узел отключился: {peer_name} ({addr[0]})")
                peer_id = payload.get("node_id")
                if isinstance(peer_id, str):
                    with self.peers_lock:
                        p = self.peers_by_id.get(peer_id)
                    if p is not None:
                        self.close_peer(p)

    def accept_loop(self) -> None:
        assert self.tcp_server is not None
        while self.running.is_set():
            try:
                sock, addr = self.tcp_server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            peer = Peer(sock, addr)
            with self.peers_lock:
                self.peers_by_sock[sock] = peer

            threading.Thread(target=self.peer_reader_loop, args=(peer,), daemon=True).start()

            # Сразу отправляем hello с нашей идентификацией
            hello = {
                "node_id": self.node_id,
                "name": self.name,
                "ip": self.bind_ip,
                "tcp_port": self.tcp_port,
            }
            self.send_frame(peer, MSG_HELLO, hello)

    def connect_to_peer(self, ip: str, port: int) -> None:
        # Защита от соединения с собой
        if ip == self.bind_ip and port == self.tcp_port:
            return

        # Если уже есть подключение к этому ip:port, второй раз не коннектимся
        with self.peers_lock:
            for p in self.peers_by_sock.values():
                if p.addr[0] == ip and p.addr[1] == port:
                    return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        try:
            sock.connect((ip, port))
        except OSError:
            sock.close()
            return
        sock.settimeout(None)

        peer = Peer(sock, (ip, port))
        with self.peers_lock:
            self.peers_by_sock[sock] = peer

        threading.Thread(target=self.peer_reader_loop, args=(peer,), daemon=True).start()

        hello = {
            "node_id": self.node_id,
            "name": self.name,
            "ip": self.bind_ip,
            "tcp_port": self.tcp_port,
        }
        self.send_frame(peer, MSG_HELLO, hello)

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

    def handle_hello(self, peer: Peer, payload: dict) -> None:
        node_id = payload.get("node_id")
        name = payload.get("name")
        if not node_id or not name:
            return

        with self.peers_lock:
            # Если этот node_id уже подключён, оставляем только одно соединение
            existing = self.peers_by_id.get(node_id)
            if existing is not None and existing is not peer:
                # детерминированное правило: оставляем соединение с меньшим fileno
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

    def broadcast_chat_payload(self, payload: dict) -> None:
        with self.peers_lock:
            peers = list(self.peers_by_sock.values())
        for peer in peers:
            self.send_frame(peer, MSG_CHAT, payload)

    def send_frame(self, peer: Peer, mtype: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        packet = FRAME_HEADER.pack(mtype, len(data)) + data
        try:
            with peer.lock:
                peer.sock.sendall(packet)
        except OSError:
            self.close_peer(peer)

    def recv_exact(self, sock: socket.socket, size: int) -> Optional[bytes]:
        chunks = []
        remaining = size
        while remaining > 0:
            try:
                chunk = sock.recv(remaining)
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv_frame(self, sock: socket.socket) -> Optional[Tuple[int, dict]]:
        header = self.recv_exact(sock, FRAME_HEADER.size)
        if header is None:
            return None
        mtype, length = FRAME_HEADER.unpack(header)
        if length < 0 or length > 10_000_000:
            return None
        payload_raw = self.recv_exact(sock, length)
        if payload_raw is None:
            return None
        try:
            payload = json.loads(payload_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return mtype, payload

    def close_peer(self, peer: Peer) -> None:
        with self.peers_lock:
            self.peers_by_sock.pop(peer.sock, None)
            if peer.node_id and self.peers_by_id.get(peer.node_id) is peer:
                self.peers_by_id.pop(peer.node_id, None)
        try:
            peer.sock.close()
        except OSError:
            pass
        if peer.name:
            self.log_event(f"TCP-соединение закрыто: {peer.name} ({peer.addr[0]}:{peer.addr[1]})")

    def print_peers(self) -> None:
        with self.peers_lock:
            peers = list(self.peers_by_id.values())
        if not peers:
            print("Нет активных TCP-соединений")
            return
        print("Активные узлы:")
        for p in peers:
            print(f"- {p.name} ({p.addr[0]}:{p.addr[1]}) id={p.node_id}")

    def print_history(self) -> None:
        with self.history_lock:
            items = list(self.history)
        if not items:
            print("История пуста")
            return
        print("История событий:")
        for line in items:
            print(line)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P2P чат (UDP discovery + TCP сообщения)")
    parser.add_argument("--name", help="Имя пользователя")
    parser.add_argument("--bind-ip", help="IP для bind (например, 127.0.0.1, 127.0.0.2)")
    parser.add_argument("--udp-port", type=int, help="UDP порт discovery")
    parser.add_argument("--tcp-port", type=int, help="TCP порт чата")
    return parser.parse_args()


def ask_if_missing(args: argparse.Namespace) -> argparse.Namespace:
    if not args.name:
        args.name = input("Введите имя: ").strip() or "anon"
    if not args.bind_ip:
        args.bind_ip = input("Введите bind IP (например, 127.0.0.1): ").strip() or "127.0.0.1"
    if not args.udp_port:
        args.udp_port = int(input("Введите UDP порт discovery (например, 50000): ").strip() or "50000")
    if not args.tcp_port:
        args.tcp_port = int(input("Введите TCP порт чата (например, 50001): ").strip() or "50001")
    return args


def print_help() -> None:
    print("Команды:")
    print("  /help                    - показать справку")
    print("  /peers                   - показать активные соединения")
    print("  /history                 - показать историю событий")
    print("  /quit                    - выйти")
    print("  <текст>                  - отправить сообщение всем узлам")


def main() -> None:
    args = ask_if_missing(parse_args())

    node = P2PChatNode(args.bind_ip, args.udp_port, args.tcp_port, args.name)
    try:
        node.start()
    except RuntimeError as exc:
        print(f"Ошибка запуска: {exc}")
        return

    print_help()

    try:
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
