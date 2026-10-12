from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager
import asyncio, sys, binascii, logging, os, uvicorn
from logging.handlers import RotatingFileHandler
import threading, re
from queue import Queue, Empty
import time
from datetime import datetime
import serial, socket
import serial.tools.list_ports as port_list

if not "linux" in sys.platform:
    import winsound

LOG_HANDLE = 'FRFID'
LOG_FILE = 'frid-log'
LOG_LEVEL = "INFO"

#  enable logging
top_log_handle = LOG_HANDLE
log = logging.getLogger(top_log_handle)
LOG_FILENAME = os.path.join(sys.path[0], f'log/{LOG_FILE}.txt')
try:
    log_level = getattr(logging, LOG_LEVEL)
except:
    log_level = getattr(logging, 'INFO')
log.setLevel(log_level)
log_handler = RotatingFileHandler(LOG_FILENAME, maxBytes=1024 * 1024, backupCount=20)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log_handler.setFormatter(log_formatter)
log.addHandler(log_handler)

# 0.15 initial version of websockets
# 0.16: when changing the administered state, return (ws event) the adminstered state anded with the operational state
# 0.17: introduced beep(), on windows use winsound.Beep(), on linux, create a separate shellscript that runs at startup.
# It checks for the creation in the /tmp directory and when present, removes it and plays a sound.  The file is created by beep()
# when required.
# 0.18: added hostname for logging
# 0.19: added logging.  Bugfixed issue with same_code_ctr
# 0.20: requirements.txt, remove version-fix to avoid problems with python 3.14 and up
# 0.21: websockets, updated protocol

version = "0.19"

class RfidScanner():
    def __init__(self):
        self.port_name = "" # e.g. /dev/ttyUSB0.  Normally, the correct port is returned (with the RFID reader attached)
        self.system_port = None # pointer to the serial port
        self.active = True
        self.os_is_linux = "linux" in sys.platform
        self.read_uid = bytearray(b'\xab\xba\x00\x10\x00\x10')
        self.resp_len = 2405
        self.prev_code = ""
        self.same_code_ctr = 0
        self.current_port_name = None
        self.log_port_disabled = True
        self.hostname = socket.gethostname()

    def beep(self):
        if self.os_is_linux:
            with open('/tmp/beep-request', 'w') as f:
                f.write('beep\n')
        else:
            winsound.Beep(1500, 200)

    def read(self): # about every 200ms
        if self.system_port and self.active:
            try:
                self.system_port.write(self.read_uid) # command to get the serial number
                rcv_raw = self.system_port.read(self.resp_len)
                if rcv_raw:
                    rcv = binascii.hexlify(rcv_raw).decode("UTF-8")
                    log.debug(f"system-port read {rcv}, prev_code {self.prev_code}, same_code_ct {self.same_code_ctr}")
                    if rcv[6:8] == "81":  # valid uid received
                        code = rcv[10:18]
                        if code != self.prev_code or self.same_code_ctr <= 0: # wait at least 2 seconds before the same badge can be scanned or continue directly when a different badge is scanned.
                            timestamp = datetime.now().isoformat()[:23]
                            self.same_code_ctr = 10 # 10 x 200ms = 2 secs
                            self.prev_code = code
                            self.beep()
                            return {"timestamp": timestamp, "code": code, "hostname": self.hostname}
                self.same_code_ctr -= 1 if self.same_code_ctr > 0 else 0
                return None
            except Exception as e:
                log.info(f"Port detattached, {e}")
            return None

    def check_usb_port(self):
        if self.os_is_linux:
            port_names = [p.name for p in port_list.comports() if "usb" in p.name.lower()]
            port_name = port_names[0] if len(port_names) > 0 else None
            self.port_name = "/dev/" + port_name if port_name else ""
        else:
            port_names = [p.description for p in list(port_list.comports()) if "ch340" in p.description.lower()]
            port_name = port_names[0] if len(port_names) > 0 else None
            if port_name:
                port_match = re.search(r"\((.*)\)", port_name)
                if port_match:
                    if not self.port_name:
                        time.sleep(1)
                    self.port_name = port_match[1]
            else:
                self.port_name = ""
        if self.port_name:
            status = True
            if self.port_name != self.current_port_name: # if the reader is switched to another port
                # Although the port is present as /dev/ttyUSBxx, it is not accessible yet.  Try a few times with a delay in between
                try_to_open_port = 10
                while try_to_open_port > 0:
                    try:
                        self.system_port = serial.Serial(self.port_name, baudrate=115200, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.1)
                        log.info(f"Set Serial port, id {self.port_name}")
                        try_to_open_port = 0
                        status = True
                    except Exception as e:
                        time.sleep(1)
                        try_to_open_port -= 1
                        if try_to_open_port <= 0:
                            log.error(f"Tried to open port {self.port_name} 10 times, did not work")
                            status = False
                self.current_port_name = self.port_name
                self.log_port_disabled = True
        else: # reader is detached
            if self.system_port:
                self.system_port.close()
            self.system_port = self.current_port_name = None
            if self.log_port_disabled:
                log.info(f"Disable Serial port")
                self.log_port_disabled = False
            time.sleep(2)
            status = False
        return status


clients = set() # each browser(tab) is a clinet
active_clients = set()
clients_lock = threading.Lock()
main_loop = None
stop_event = threading.Event()
receive_queue = Queue()

# Accesses the RFID scanner via the serial/USB interface.
# It is a separate thread because it is not sure if the serial library is blocking or not.  If it is blocking, using async would block the whole program.
# It cannot use websockets directly, so it uses a lock to hand over the scanned RFID to ws_sender.
def serial_worker():
    global main_loop
    check_usb_port_ctr = 0 # every loop takes 0.2 sec.  usb-port is checked every 10 * 0.2 sec (2 sec)
    previous_scanner_state = False
    rfid_scanner = RfidScanner()

    while not stop_event.is_set():
        cycle_start = datetime.now()
        if check_usb_port_ctr > 10:
            scanner_state = rfid_scanner.check_usb_port()
            if scanner_state != previous_scanner_state:
                previous_scanner_state = scanner_state
                data = {"scanner_state": {"state": scanner_state}}
                log.info(f"ws queue {data}")
                main_loop.call_soon_threadsafe(broadcast, data)
            check_usb_port_ctr = 0
        read_result = rfid_scanner.read()
        if read_result is not None:
            data = {"read": read_result}
            log.info(f"ws queue {data}")
            main_loop.call_soon_threadsafe(broadcast, data)
        check_usb_port_ctr += 1
        try:
            status = receive_queue.get_nowait()
            log.info(f"ws received {{'status': {status}}}")
            rfid_scanner.active = status
            data = {"scanner_state": {"state": previous_scanner_state and rfid_scanner.active}}
            main_loop.call_soon_threadsafe(broadcast, data)
        except Empty:
            pass
        cycle_delta = (datetime.now() - cycle_start).microseconds / 1000 # number of milliseconds
        if cycle_delta < 200:
            time.sleep((200 - cycle_delta) / 1000)

# execute at startup and shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup, start serial thread
    global main_loop
    main_loop = asyncio.get_running_loop()
    log.info("Starting serial worker thread")
    thread = threading.Thread(target=serial_worker, daemon=True)
    thread.start()

    try:
        yield
    finally:
        # shutdown
        log.info("Stopping serial worker thread")
        stop_event.set()
        thread.join(timeout=2)

app = FastAPI(lifespan=lifespan)

# Each connected client gets its own unbounded queue. The serial worker schedules
# broadcasts on the asyncio loop, and each sender drains its client's queue.
def broadcast(data):
    with clients_lock:
        for queue in tuple(clients):
            queue.put_nowait(data)


async def ws_sender(ws: WebSocket, queue: asyncio.Queue):
    try:
        while True:
            data = await queue.get()
            await ws.send_json(data)
            log.info(f"ws sent {data}")
    except Exception as e:
        log.info(f"WebSocket sender stopped: {e}")


# for each client, add a queue and a task
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    queue = asyncio.Queue()
    with clients_lock:
        clients.add(queue)
    task = asyncio.create_task(ws_sender(ws, queue))

    try:
        while True:
            data = await ws.receive_json()
            if "status" in data:
                was_active = bool(active_clients)
                if data["status"]:
                    active_clients.add(queue)
                else:
                    active_clients.discard(queue)
                is_active = bool(active_clients)
                if is_active != was_active:
                    receive_queue.put(is_active)
    except WebSocketDisconnect:
        pass
    finally:
        was_active = bool(active_clients)
        active_clients.discard(queue)
        if bool(active_clients) != was_active:
            receive_queue.put(bool(active_clients))
        with clients_lock:
            clients.discard(queue)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8765, ssl_keyfile="localhost-key.pem", ssl_certfile="localhost.pem")
