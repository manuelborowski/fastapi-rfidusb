import time
import urllib.parse

from fastapi import FastAPI
import threading, logging, glob
import sys, os,  serial, re, requests, binascii, datetime
import serial.tools.list_ports as port_list
from config import LOG_HANDLE, LOG_FILE, LOG_LEVEL, BR_URL, BR_KEY

#  enable logging
top_log_handle = LOG_HANDLE
log = logging.getLogger(top_log_handle)

LOG_FILENAME = os.path.join(sys.path[0], f'log/{LOG_FILE}.txt')
try:
    log_level = getattr(logging, LOG_LEVEL)
except:
    log_level = getattr(logging, 'INFO')
log.setLevel(log_level)
log_handler = logging.handlers.RotatingFileHandler(LOG_FILENAME, maxBytes=1024 * 1024, backupCount=20)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log_handler.setFormatter(log_formatter)
log.addHandler(log_handler)

# 0.1 initial version
# 0.2: upgrade serial port handling
# 0.3: small bugfix
# 0.4: get/set api key and url

version = "0.3"

#linux beep:
# sudo apt install beep
# sudo usermod -aG input badgereader
#linux ch340 serial:
# sudo apt autoremove brltty
# sudo usermod -aG dialout badgereader


log.info("start")

app = FastAPI()

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
        self.ctr = 0
        self.prev_code = ""

    @property
    def port(self):
        return self.__port

    @port.setter
    def port(self, value):
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

    def kick(self): # about 100ms
        if self.__port and self.__location:
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
                                ret = requests.post(f"{self.__url}/api/registration/add", headers={'x-api-key': self.__api_key}, json={"location_key": self.__location, "badge_code": code, "timestamp": timestamp})
                            except Exception as e:
                                log.error(f"requests.post() threw exception: {e}")
                                return
                            if ret.status_code == 200:
                                res = ret.json()
                                if res["status"]:
                                    log.info(f"OK, {code} at {timestamp}")
                                    if os_linux:
                                        os.system("beep -f 1500 -l 200")
                                    else:
                                        winsound.Beep(1500, 200)
                                else:
                                    log.error(f"FOUT, {code} at {timestamp}")
                                    if os_linux:
                                        os.system("beep -f 1500 -l 800")
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
        self.usbport_ctr = 0
        self.update_ctr = 0
        self.__port_id = ""
        self.__location = ""
        self.lock = threading.Lock()
        self.rfid = Rfid7941W()
        t = threading.Thread(target=self.run)
        t.start()

    def run(self):
        current_port_id = None
        port_id = None
        detached_ctr = 0
        while True:
            self.rfid.kick()
            self.usbport_ctr += 1
            self.update_ctr += 1
            if self.usbport_ctr >= 20:
                self.usbport_ctr = 0
                if os_linux:
                    port_names = [p.name for p in port_list.comports() if "usb" in p.name.lower()]
                    port_name = port_names[0] if len(port_names) > 0 else None
                    port_id = "/dev/" + port_name if port_name else None
                else:
                    port_names = [p.description for p in list(port_list.comports()) if "ch340" in p.description.lower()]
                    port_name = port_names[0] if len(port_names) > 0 else None
                    if port_name:
                        port_match = re.search(r"\((.*)\)", port_name)
                        if port_match:
                            if not port_id:
                                time.sleep(1)
                            port_id = port_match[1]
                    else:
                        port_id = None
                if port_id:
                    if port_id != current_port_id:
                        self.serial_port = serial.Serial(port_id, baudrate=115200, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.1)
                        log.info(f"Set Serial port, id {port_id}")
                        current_port_id = port_id
                        detached_ctr = 0
                else:
                    self.serial_port = current_port_id = None
                    detached_ctr += 1
                    if detached_ctr > 400000:
                        log.info(f"Disable Serial port")
                        detached_ctr = 0
                self.rfid.port = self.serial_port
                self.__port_id = port_id if port_id else ""


    @property
    def port(self):
        self.lock.acquire()
        port_id = self.__port_id
        self.lock.release()
        return port_id

    @property
    def location(self):
        self.lock.acquire()
        location = self.rfid.location
        self.lock.release()
        return location

    @location.setter
    def location(self, value):
        self.lock.acquire()
        log.info(f"Set location, {value}")
        self.rfid.location = value
        self.lock.release()

    @property
    def url(self):
        self.lock.acquire()
        url = self.rfid.url
        self.lock.release()
        return url

    @url.setter
    def url(self, value):
        self.lock.acquire()
        self.rfid.url = value
        log.info(f"Set url, {value}")
        self.lock.release()

    @property
    def api_key(self):
        self.lock.acquire()
        key = "xxxx"
        self.lock.release()
        return key

    @api_key.setter
    def api_key(self, value):
        self.lock.acquire()
        self.rfid.api_key = value
        log.info(f"Set api_key")
        self.lock.release()


server = BadgeServer()
server.init()

@app.get("/serial_port")
async def get_serial_port():
    return {"port": server.port}


@app.get("/location")
async def get_location():
    return {"location": server.location}


@app.post("/location/{location}")
def set_location(location):
    server.location = location
    return "ok"


@app.get("/url")
async def get_url():
    return {"url": server.url}


@app.post("/url/{url}")
def set_location(url):
    url = urllib.parse.unquote(url)
    url = urllib.parse.unquote(url)
    server.url = url
    return "ok"


@app.get("/api_key")
async def get_api_key():
    return {"api_key": server.api_key}


@app.post("/api_key/{key}")
def set_api_key(key):
    server.api_key = key
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

