from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager
import asyncio, sys, binascii, logging, os, uvicorn
from logging.handlers import RotatingFileHandler
import threading, re
import time
from datetime import datetime
import serial
import serial.tools.list_ports as port_list

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

version = "0.15"

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

    def read(self): # about every 200ms
        if self.system_port and self.active:
            try:
                self.system_port.write(self.read_uid) # command to get the serial number
                rcv_raw = self.system_port.read(self.resp_len)
                if rcv_raw:
                    rcv = binascii.hexlify(rcv_raw).decode("UTF-8")
                    if rcv[6:8] == "81":  # valid uid received
                        code = rcv[10:18]
                        if code != self.prev_code or self.same_code_ctr <= 0: # wait at least 2 seconds before the same badge can be scanned or continue directly when a different badge is scanned.
                            timestamp = datetime.now().isoformat()[:23]
                            self.same_code_ctr = 10 # 10 x 200ms = 2 secs
                            self.prev_code = code
                            return {"timestamp": timestamp, "code": code}
                        self.same_code_ctr -= 1
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


global_send_data = None
global_send_data_available = False
global_receive_data = None
global_receive_data_available = False
lock = threading.Lock()
stop_event = threading.Event()

# Accesses the RFID scanner via the serial/USB interface.
# It is a separate thread because it is not sure if the serial library is blocking or not.  If it is blocking, using async would block the whole program.
# It cannot use websockets directly, so it uses a lock to hand over the scanned RFID to ws_sender.
def serial_worker():
    global global_send_data
    global global_send_data_available
    global global_receive_data
    global global_receive_data_available
    check_usb_port_ctr = 0 # every loop takes 0.2 sec.  usb-port is checked every 10 * 0.2 sec (2 sec)
    previous_scanner_state = None
    rfid_scanner = RfidScanner()

    while not stop_event.is_set():
        cycle_start = datetime.now()
        if check_usb_port_ctr > 10:
            scanner_state = rfid_scanner.check_usb_port()
            if scanner_state != previous_scanner_state:
                previous_scanner_state = scanner_state
                with lock:
                    global_send_data_available = True
                    global_send_data = {"scanner_state": {"state": scanner_state}}
            check_usb_port_ctr = 0
        read_result = rfid_scanner.read()
        if read_result is not None:
            with lock:
                global_send_data_available = True
                global_send_data = {"read": read_result}
        check_usb_port_ctr += 1
        with lock:
            if global_receive_data_available:
                global_receive_data_available = False
                log.info(f"ws received {global_receive_data}")
                if "status" in global_receive_data:
                    rfid_scanner.active = global_receive_data["status"]
        cycle_delta = (datetime.now() - cycle_start).microseconds / 1000 # number of milliseconds
        if cycle_delta < 200:
            time.sleep((200 - cycle_delta) / 1000)

# execute at startup and shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup, start serial thread
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

# async cannot block (i.e. cannot use blocking libraries), therefore use a lock to sync data between serial_worker and this function.
async def ws_sender(ws: WebSocket):
    global global_send_data_available
    try:
        data = None
        while True:
            await asyncio.sleep(1)
            with lock:
                if global_send_data_available:
                    global_send_data_available = False
                    data = global_send_data
            if data is not None:
                await ws.send_json(data)
                data = None
    except Exception:
        pass


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global global_receive_data
    global global_receive_data_available
    await ws.accept()
    task = asyncio.create_task(ws_sender(ws))

    try:
        while True:
            data = await ws.receive_json()
            with lock:
                global_receive_data_available = True
                global_receive_data = data
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()


if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8765, ssl_keyfile="localhost-key.pem", ssl_certfile="localhost.pem")
