import time
import urllib.parse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import threading, logging, glob
import sys, os,  serial, re, requests, binascii, datetime
import serial.tools.list_ports as port_list
from config import LOG_HANDLE, LOG_FILE, LOG_LEVEL, BR_URL, BR_KEY
from logging.handlers import RotatingFileHandler

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

# 0.1 initial version
# 0.2: upgrade serial port handling
# 0.3: small bugfix
# 0.4: get/set api key and url
# 0.5: added cors.  Added api to activate/deactivate
# 0.6: added requirements.txt
# 0.8: bugfix linux beep command
# 0.9: bugfix log-handler.  Uninstall serial-module.


version = "0.9"

#linux beep:
# sudo apt install beep
# sudo usermod -aG input badgereader
#linux ch340 serial:
# sudo apt autoremove brltty
# sudo usermod -aG dialout badgereader


log.info("start")

app = FastAPI()
origins = ["*",]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"])

#  uvicorn.exe rfidusb:app

os_linux = "linux" in sys.platform

if not os_linux:
    import winsound

class Rfid7941W():
    read_uid = bytearray(b'\xab\xba\x00\x10\x00\x10')
    resp_len = 2405

    def __init__(self):
        self.__port = None
        self.__location = None
        self.__url = BR_URL
        self.__api_key = BR_KEY
        self.__active = False
        self.ctr = 0
        self.prev_code = ""

    @property
    def system_port(self):
        return self.__port

    @system_port.setter
    def system_port(self, value):
        self.__port = value

    @property
    def location(self):
        return self.__location

    @location.setter
    def location(self, value):
        self.__location = value

    @property
    def url(self):
        return self.__url

    @url.setter
    def url(self, value):
        self.__url = value

    @property
    def api_key(self):
        return self.__api_key

    @api_key.setter
    def api_key(self, value):
        self.__api_key = value

    @property
    def active(self):
        return self.__active

    @active.setter
    def active(self, value):
        self.__active = value

    def kick(self): # about 100ms
        if self.__port and self.__location and self.__active:
            try:
                self.__port.write(self.read_uid)
                rcv_raw = self.__port.read(self.resp_len)
                if rcv_raw:
                    rcv = binascii.hexlify(rcv_raw).decode("UTF-8")
                    if rcv[6:8] == "81":  # valid uid received
                        code = rcv[10:18]
                        if code != self.prev_code or self.ctr > 5:
                            timestamp = datetime.datetime.now().isoformat().split(".")[0]
                            try:
                                ___start = datetime.datetime.now()
                                ret = requests.post(f"{self.__url}/api/registration/add", headers={'x-api-key': self.__api_key}, json={"location_key": self.__location, "badge_code": code, "timestamp": timestamp})
                                log.info(f"request: {datetime.datetime.now() - ___start}")
                            except Exception as e:
                                log.error(f"requests.post() threw exception: {e}")
                                return
                            if ret.status_code == 200:
                                res = ret.json()
                                if res["status"]:
                                    log.info(f"OK, {code} at {timestamp}")
                                    if os_linux:
                                        os.system("/usr/bin/beep -f 1500 -l 200")
                                    else:
                                        winsound.Beep(1500, 200)
                                else:
                                    log.error(f"FOUT, {code} at {timestamp}")
                                    if os_linux:
                                        os.system("/usr/bin/beep -f 1500 -l 800")
                                    else:
                                        winsound.Beep(1500, 800)
                            self.ctr = 0
                        self.prev_code = code
                        self.ctr += 1
            except Exception as e:
                log.info(f"Port detattached, {e}")
        # time.sleep(0.1)


class BadgeServer():

    def init(self):
        self.__port_name = ""
        self.__location = ""
        self.__api_key = ""
        self.__url = ""
        self.__active = False
        self.lock = threading.Lock()
        self.rfid = Rfid7941W()
        t = threading.Thread(target=self.run)
        t.start()

    def run(self):
        current_port_name = None
        system_port = None
        usbport_ctr = 0
        log_port_disabled = True
        while True:
            self.rfid.kick()
            usbport_ctr += 1
            if usbport_ctr >= 20:
                self.lock.acquire()
                usbport_ctr = 0
                # time.sleep(1)
                if os_linux:
                    port_names = [p.name for p in port_list.comports() if "usb" in p.name.lower()]
                    port_name = port_names[0] if len(port_names) > 0 else None
                    self.__port_name = "/dev/" + port_name if port_name else ""
                else:
                    port_names = [p.description for p in list(port_list.comports()) if "ch340" in p.description.lower()]
                    port_name = port_names[0] if len(port_names) > 0 else None
                    if port_name:
                        port_match = re.search(r"\((.*)\)", port_name)
                        if port_match:
                            if not self.__port_name:
                                time.sleep(1)
                            self.__port_name = port_match[1]
                    else:
                        self.__port_name = ""
                if self.__port_name:
                    if self.__port_name != current_port_name:
                        # Although the port is present as /dev/ttyUSBxx, it is not accessible yet.  Try a few times with a delay in between
                        try_to_open_port = 10
                        while try_to_open_port > 0:
                            try:
                                system_port = serial.Serial(self.__port_name, baudrate=115200, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.1)
                                log.info(f"Set Serial port, id {self.__port_name}")
                                try_to_open_port = 0
                            except Exception as e:
                                time.sleep(1)
                                try_to_open_port -= 1
                                if try_to_open_port <= 0:
                                    log.error(f"Tried to open port {self.__port_name} 10 times, did not work")
                        current_port_name = self.__port_name
                        log_port_disabled = True
                else:
                    if system_port:
                        system_port.close()
                    system_port = current_port_name = None
                    if log_port_disabled:
                        log.info(f"Disable Serial port")
                        log_port_disabled = False
                self.rfid.system_port = system_port
                self.rfid.location = self.__location
                self.rfid.url = self.__url
                self.rfid.api_key = self.__api_key
                self.rfid.active = self.__active
                # log.info(f", {self.rfid.location}, {self.rfid.url}, {self.rfid.api_key}, {self.rfid.active}, {self.rfid.system_port} ")
                self.lock.release()


    @property
    def port(self):
        self.lock.acquire()
        port_id = self.__port_name
        self.lock.release()
        return port_id

    @property
    def location(self):
        return "NA"


    @location.setter
    def location(self, value):
        self.lock.acquire()
        log.info(f"Set location, {value}")
        self.__location = value
        self.lock.release()

    @property
    def url(self):
        return "NA"


    @url.setter
    def url(self, value):
        self.lock.acquire()
        self.__url = value
        log.info(f"Set url, {value}")
        self.lock.release()

    @property
    def api_key(self):
        return "NA"


    @api_key.setter
    def api_key(self, value):
        self.lock.acquire()
        self.__api_key = value
        log.info(f"Set api_key")
        self.lock.release()

    @property
    def active(self):
        return "NA"


    @active.setter
    def active(self, value):
        self.lock.acquire()
        self.__active = value
        log.info(f"Set active {value}")
        self.lock.release()


server = BadgeServer()
server.init()

@app.get("/serial_port")
async def get_serial_port():
    return {"port": server.port}


@app.post("/location/{location}")
def set_location(location):
    server.location = location
    return "ok"


@app.post("/url/{url}")
def set_location(url):
    url = urllib.parse.unquote(url)
    url = urllib.parse.unquote(url)
    server.url = url
    return "ok"


@app.post("/api_key/{key}")
def set_api_key(key):
    server.api_key = key
    return "ok"


@app.post("/active/{setting}")
def set_active(setting):
    server.active = setting == "1"
    return "ok"


@app.get("/version")
def get_version():
    return {"version": version}


@app.get("/update/{versions}")
def get_update(versions):
    try:
        versions = versions.split("-")
        first_version = float(versions[0])
        last_version = float(versions[1])
        log.info(f"Get version from {first_version} till {last_version}")
        files = os.listdir("update")
        all_prefixes = [float(s.split("-")[0]) for s in files if "-" in s]
        versions = [p for p in all_prefixes if p >= first_version and p <= last_version]
        update_files = []
        for version in versions:
            if f"{version}-update.sql" in files:
                content = open(f"update/{version}-update.sql", "r").read()
                update_files.append(("sql", content))
            elif f"{version}-config.py" in files:
                content = open(f"update/{version}-config.py", "r").read()
                update_files.append(("config", content))
            elif f"{version}-bash.sh" in files:
                content = open(f"update/{version}-bash.sh", "r").read()
                update_files.append(("shell", content))
        if "bash.sh" in files:
            content = open(f"update/bash.sh", "r").read()
            update_files.append(("shell", content))
        return {"status": True, "data": update_files}
    except Exception as e:
        return {"status": False, "data": f"Wrong versions string (x.y-w.z), erorr {e}"}

