# Полное объяснение кода + соответствие пунктам задания

Файл с кодом: `p2p_chat.py`.

Ниже: что делает программа, как работает протокол, и где в коде выполняются требования из `P2P чат.pdf`.

---

## 1) Что по заданию нужно сделать

Из `P2P чат.pdf`:

- **Одноранговый чат** в локальной сети (каждый узел = участник).
- Узел идентифицируется **IP-адресом** и **именем** (имя задаёт пользователь).
- **UDP discovery**:
  - после запуска узел отправляет **широковещательный** UDP-пакет со своим именем;
  - другие узлы, получив такой пакет, устанавливают с отправителем **TCP-соединение** и передают по нему **своё имя**.
- **TCP чат**:
  - каждый узел поддерживает TCP-соединение с другими узлами;
  - сообщения отправляются всем узлам («логически общее пространство»).
- Отключение узла корректно обрабатывается.
- UI не должен блокироваться при сетевой работе.
- История событий с отметками времени:
  - входящие сообщения (имя + IP отправителя);
  - собственные отправленные;
  - обнаружение нового узла;
  - отключение узла.

Доп. задание (передача истории при подключении) — **не реализовано** и удалено.

---

## 2) Общая схема работы программы

Каждый экземпляр программы одновременно делает 3 вещи:

1. **Слушает UDP** на `bind_ip:udp_port` и принимает discovery/leave.
2. **Слушает TCP** на `bind_ip:tcp_port` и принимает входящие TCP-подключения.
3. Даёт пользователю вводить текст в консоль и отправляет его всем активным TCP-пирам.

Сеть работает в фоновых потоках, поэтому пользовательский ввод не блокируется.

---

## 3) Где в коде выполняются пункты задания

### 3.1. Имя узла задаётся пользователем

- Аргументы командной строки: `--name`.
- Если не передали — спрашиваем интерактивно.

Место в коде:

```424:442:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
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
        args.tcp_port = int(input("Введите TCP порт чата (например, 50001): ").strip() or "50001"
    )
    return args
```

(Если будешь показывать преподавателю — скажи, что имя не обязано быть уникальным.)

### 3.2. IP и порт не хардкодим, предоставляем ввод

- `--bind-ip`, `--udp-port`, `--tcp-port` или интерактивный ввод (см. блок выше).

### 3.3. Проверка доступности порта при создании сокета (bind)

Если порт занят/адрес недоступен — `bind()` бросает `OSError`, мы выводим понятную ошибку.

UDP:

```109:121:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
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
```

TCP:

```122:135:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
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
```

### 3.4. UDP broadcast: уведомление о подключении

После запуска отправляется UDP пакет типа `discover` с именем узла и tcp_port.

```76:87:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def start(self) -> None:
        self.start_udp_listener()
        self.start_tcp_server()

        threading.Thread(target=self.udp_discovery_loop, daemon=True).start()

        # Первичное объявление о себе
        self.send_discovery_broadcast()
```

Отправка broadcast:

```137:158:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
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
        self.udp_sock.sendto(data, ("255.255.255.255", self.udp_port))
```

### 3.5. Получили discovery → устанавливаем TCP и передаём имя

Когда другой узел получает UDP `discover`, он делает TCP connect к отправителю.

```174:205:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def udp_discovery_loop(self) -> None:
        ...
            if mtype == "discover":
                peer_name = payload.get("name", "unknown")
                peer_tcp = payload.get("tcp_port")
                ...
                self.log_event(f"Обнаружен узел: {peer_name} ({addr[0]}:{peer_tcp})")
                self.connect_to_peer(addr[0], peer_tcp)
```

После установки TCP обе стороны отправляют `HELLO` (тип 2) с `name` и `node_id`:

```206:263:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def accept_loop(self) -> None:
        ...
            hello = {
                "node_id": self.node_id,
                "name": self.name,
                "ip": self.bind_ip,
                "tcp_port": self.tcp_port,
            }
            self.send_frame(peer, MSG_HELLO, hello)

    def connect_to_peer(self, ip: str, port: int) -> None:
        ...
        self.send_frame(peer, MSG_HELLO, hello)
```

А обработка hello записывает имя узла:

```276:307:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def handle_hello(self, peer: Peer, payload: dict) -> None:
        node_id = payload.get("node_id")
        name = payload.get("name")
        ...
        peer.node_id = node_id
        peer.name = name
        self.peers_by_id[node_id] = peer
```

### 3.6. Обмен сообщениями по TCP + своё сообщение всем

Пользователь вводит строку в `main()`. Если это не команда — вызывается `send_chat(text)`.

```456:509:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def main() -> None:
        ...
            if line == "/quit":
                break
            ...
            node.send_chat(line)
```

`send_chat` формирует payload и шлёт его всем подключённым пирам:

```320:349:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
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
        ...
        for peer in peers:
            self.send_frame(peer, MSG_CHAT, payload)
```

Входящие сообщения принимаются в `peer_reader_loop` и обрабатываются в `handle_chat`, где пишется история:

```265:317:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def peer_reader_loop(self, peer: Peer) -> None:
        ...
                if mtype == MSG_HELLO:
                    self.handle_hello(peer, payload)
                elif mtype == MSG_CHAT:
                    self.handle_chat(peer, payload)

    def handle_chat(self, peer: Peer, payload: dict) -> None:
        ...
        self.log_event(f"{sender_name} ({sender_ip}): {text}")
```

### 3.7. Собственный формат сообщений для TCP (фрейминг)

В задании рекомендовано сделать свой формат: тип + длина + payload.

У нас:
- 1 байт: тип сообщения
- 4 байта: длина
- дальше JSON

Место в коде:

```14:31:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
MSG_CHAT = 1
MSG_HELLO = 2

FRAME_HEADER = struct.Struct("!BI")  # type (1 byte), length (4 bytes)
```

Отправка:

```352:360:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def send_frame(self, peer: Peer, mtype: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        packet = FRAME_HEADER.pack(mtype, len(data)) + data
        peer.sock.sendall(packet)
```

Приём:

```361:389:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def recv_exact(self, sock: socket.socket, size: int) -> Optional[bytes]:
        ...

    def recv_frame(self, sock: socket.socket) -> Optional[Tuple[int, dict]]:
        header = self.recv_exact(sock, FRAME_HEADER.size)
        mtype, length = FRAME_HEADER.unpack(header)
        payload_raw = self.recv_exact(sock, length)
        payload = json.loads(payload_raw.decode("utf-8"))
        return mtype, payload
```

### 3.8. UI не блокируется

- Ввод пользователя — в главном потоке (`input()` в `main`).
- UDP приём — отдельный поток `udp_discovery_loop`.
- TCP accept — отдельный поток `accept_loop`.
- На каждого пира отдельный поток `peer_reader_loop`.

За счёт этого сеть не мешает вводу.

### 3.9. История событий

Запись событий делается функцией `log_event()` — она добавляет таймстамп и печатает строку.

```66:75:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def log_event(self, text: str) -> None:
        line = f"[{self.now()}] {text}"
        with self.history_lock:
            self.history.append(line)
        print(line)
```

События, которые попадают в историю:
- обнаружение узла (UDP discover): `log_event("Обнаружен узел...")`
- установка TCP: `log_event("TCP-соединение установлено...")`
- входящее сообщение: `log_event("Имя (IP): текст")`
- исходящее сообщение: `log_event("Я: ...")`
- закрытие TCP: `log_event("TCP-соединение закрыто...")`
- отключение узла (UDP leave): `log_event("Узел отключился...")`

Команда просмотра истории:

```413:421:/Users/volkov_sh/Desktop/Ксис лабы/лаб3/p2p_chat.py
    def print_history(self) -> None:
        ...
```

---

## 4) Что считается «удалённым доп. заданием»

В коде **нет**:
- запросов истории у других узлов;
- передачи истории по TCP новому узлу;
- отображения полученной истории как отдельной процедуры синхронизации.

(История есть только локально в памяти с момента запуска — как требует основное задание.)
