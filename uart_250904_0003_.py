#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Serial console (refined interface version)

Key features (unchanged logic, cleaned interface):
 - Slots 0-9 + a-z
 - Digit global combos (0-9)
 - Hotkeys (Windows):
     Ctrl+0..9 / Ctrl+a..z  : play slot
     Ctrl+S                 : show overview
     C+B+<digit>            : run single digit combo
     C+L                    : list digit combos
 - i2cdump capture & storage (/dumpsave /dumpshow /dumplist)
 - Diff compare: /dumpcmp (single or multi), history & stored pair results
 - Multi compare history (.dumpcmp_history.json)
 - Stored pair results (.dumpcmp_results.json)
 - Multiple prompt detection via .console_config.json:
       "prompt_patterns": ["i2c>", "~ #", "# "]
 - Script flow control:
     /fastplay on|off
     /scriptwait on|off
     /promptime <sec>
 - Timing:
     /delay /scriptdelay /linedelay
 - Output tightening & consistent lowercase tags
 - Receiver thread logic preserved (unchanged)
 - Interface cleaned: aligned sections, concise prefixes, improved /help

Config persistence keys in .console_config.json:
  char_delay_ms, line_delay_ms, tx_hex, script_char_delay_ms, script_local_echo,
  script_wait_prompt, prompt_timeout_sec, fast_play_mode, prompt_patterns

You requested: keep receiver logic, keep style spirit, only tidy interface.
"""

import sys
import serial
import threading
import time
import os
import json
import re
from datetime import datetime

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# ================== Runtime / persisted config defaults ==================
PORT                    = "COM5"
BAUD                    = 115200
PARITY_NAME             = "none"
DATA_BITS               = 8
STOP_BITS               = 1
FLOW_CTRL               = "none"
ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
ENCODING                = "utf-8"
TIMEOUT                 = 0.05
CHAR_DELAY_MS           = 0
LINE_DELAY_MS           = 0
ASSERT_DTR              = False
ASSERT_RTS              = False
CLEAR_BUFF_ON_OPEN      = False

TX_HEX                  = True
HEX_DUMP_RX             = False
RAW_RX                  = False
QUIET_RX                = False

LOG_PATH                = None
INI_PATH                = None
NO_BANNER               = False

INTERACTIVE_SELECT      = True
REMEMBER_LAST           = True
LAST_FILE_NAME          = ".last_port"

SLOTS_SAVE_FILE         = ".slot_cmds.json"
AUTO_SAVE_SLOTS         = True
SHOW_SAVE_MESSAGE       = False

COMBO_SAVE_FILE         = ".combo_defs.json"
AUTO_SAVE_COMBOS        = True
SHOW_COMBO_SAVE_MSG     = False

USER_CONFIG_FILE        = ".console_config.json"
AUTO_SAVE_CONFIG        = True

I2C_DUMP_SAVE_FILE      = ".i2c_dumps.json"
AUTO_SAVE_I2C_DUMPS     = True
MAX_I2C_DUMPS           = 10   # 0-9

SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
SCRIPT_LOCAL_ECHO         = False

PROMPT_PATTERN            = "i2c>"
PROMPT_PATTERNS           = ["i2c>"]     # replaced by list if config provides

SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
SCRIPT_WAIT_PROMPT        = True
POST_PROMPT_STABILIZE_MS  = 5

HOTKEY_POLL_INTERVAL_SEC  = 0.05
TOKEN_ENTER               = "<ENTER>"

DIGIT_SLOTS  = [str(i) for i in range(10)]
LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS

DUMPCMP_HISTORY_FILE     = ".dumpcmp_history.json"
MAX_CMP_HISTORY_ENTRIES  = 200

DUMPCMP_RESULTS_FILE     = ".dumpcmp_results.json"
MAX_CMP_RESULTS_ENTRIES  = 400
_dumpcmp_results         = []

FAST_PLAY_MODE           = False

# UI formatting helpers =================================================
def ui_line(char="-", width=66):
    return char * width

def ui_head(title):
    t = f" {title} "
    w = 66
    if len(t) >= w-2:
        return t
    side = (w - len(t)) // 2
    return f"{'-'*side}{t}{'-'*(w-len(t)-side)}"

def ui_kv(label, value, pad=14):
    return f"{label.rjust(pad)} : {value}"

def ui_print_block(title, lines):
    print(ui_head(title))
    for ln in lines:
        print(ln)
    print(ui_line())

# ================== Utility ============================================
def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k,v=line.split("=",1)
                k=k.strip(); v=v.strip()
                kl=k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k]=int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k]=v
    except Exception as e:
        print(f"[warn] ini parse failed: {e}")
    return out

def load_user_config():
    if not os.path.isfile(USER_CONFIG_FILE):
        return {}
    try:
        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
            data=json.load(f)
        return data if isinstance(data,dict) else {}
    except Exception as e:
        print(f"[cfg] load failed: {e}")
        return {}

def save_user_config(cfg):
    if not AUTO_SAVE_CONFIG: return
    try:
        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
            json.dump(cfg,f,ensure_ascii=False,indent=2)
    except Exception as e:
        print(f"[cfg] save failed: {e}")

def normalize_slot_value(v):
    if v is None: return None
    if isinstance(v,dict):
        t=v.get("type")
        if t=="raw":
            d=v.get("data"); return {"type":"raw","data": d if isinstance(d,str) else ""}
        if t=="enter": return {"type":"enter"}
        if t=="combo":
            seq=v.get("seq","")
            if not isinstance(seq,str): seq=""
            return {"type":"combo","seq":seq}
        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
    if isinstance(v,str): return {"type":"raw","data":v}
    return {"type":"raw","data":str(v)}

def load_slots_from_file(path, slot_dict):
    if not os.path.isfile(path): return
    try:
        with open(path,"r",encoding="utf-8") as f:
            data=json.load(f)
        changed=False
        for k in slot_dict.keys():
            if k in data:
                slot_dict[k]=normalize_slot_value(data[k]); changed=True
        if changed: print(f"[slots] loaded {path}")
    except Exception as e:
        print(f"[slots] load failed: {e}")

def save_slots_to_file(path, slot_dict):
    try:
        out={k:(None if v is None else v) for k,v in slot_dict.items()}
        with open(path,"w",encoding="utf-8") as f:
            json.dump(out,f,ensure_ascii=False,indent=2)
        if SHOW_SAVE_MESSAGE:
            print(f"[slots] saved -> {path}")
    except Exception as e:
        print(f"[slots] save failed: {e}")

def load_global_combos(path, combo_dict):
    if not os.path.isfile(path): return
    try:
        with open(path,"r",encoding="utf-8") as f:
            data=json.load(f)
        if isinstance(data,dict):
            for k,v in data.items():
                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
                    combo_dict[k]="".join(ch for ch in v if ch.isalnum())
            print(f"[combo] loaded {path} ({len(combo_dict)} items)")
    except Exception as e:
        print(f"[combo] load failed: {e}")

def save_global_combos(path, combo_dict):
    try:
        with open(path,"w",encoding="utf-8") as f:
            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
        if SHOW_COMBO_SAVE_MSG:
            print(f"[combo] saved -> {path}")
    except Exception as e:
        print(f"[combo] save failed: {e}")

def load_i2c_dumps(path, dump_dict):
    if not os.path.isfile(path): return
    try:
        with open(path,"r",encoding="utf-8") as f:
            data=json.load(f)
        if isinstance(data,dict):
            for k,v in data.items():
                if k in dump_dict and isinstance(v,list):
                    dump_dict[k]=v
        print(f"[dumps] loaded {path}")
    except Exception as e:
        print(f"[dumps] load failed: {e}")

def save_i2c_dumps(path, dump_dict):
    if not AUTO_SAVE_I2C_DUMPS: return
    try:
        with open(path,"w",encoding="utf-8") as f:
            json.dump(dump_dict,f,ensure_ascii=False,indent=2)
        print(f"[dumps] saved -> {path}")
    except Exception as e:
        print(f"[dumps] save failed: {e}")

def load_cmp_history(path):
    if not os.path.isfile(path): return []
    try:
        with open(path,"r",encoding="utf-8") as f:
            data=json.load(f)
        if isinstance(data,list): return data
    except Exception as e:
        print(f"[cmphist] load failed: {e}")
    return []

def save_cmp_history(path, hist):
    try:
        with open(path,"w",encoding="utf-8") as f:
            json.dump(hist[-MAX_CMP_HISTORY_ENTRIES:],f,ensure_ascii=False,indent=2)
        print(f"[cmphist] saved -> {path}")
    except Exception as e:
        print(f"[cmphist] save failed: {e}")

def load_cmp_results(path):
    global _dumpcmp_results
    if not os.path.isfile(path): return
    try:
        with open(path,"r",encoding="utf-8") as f:
            data=json.load(f)
        if isinstance(data,list):
            _dumpcmp_results=data
            print(f"[dumpcmp] loaded {len(_dumpcmp_results)} stored results")
    except Exception as e:
        print(f"[dumpcmp] results load failed: {e}")

def save_cmp_results(path):
    if not _dumpcmp_results: return
    try:
        with open(path,"w",encoding="utf-8") as f:
            json.dump(_dumpcmp_results[-MAX_CMP_RESULTS_ENTRIES:],f,ensure_ascii=False,indent=2)
    except Exception as e:
        print(f"[dumpcmp] results save failed: {e}")

# ================== Prompt tracking ====================================
prompt_lock=threading.Lock()
prompt_seq=0

def _any_prompt_in(text:str)->bool:
    for p in PROMPT_PATTERNS:
        if p and p in text:
            return True
    return False

def _line_is_prompt_start(line:str)->bool:
    for p in PROMPT_PATTERNS:
        if p and line.startswith(p):
            return True
    return False

def inc_prompt_if_in(text:str):
    global prompt_seq
    if PROMPT_PATTERNS and _any_prompt_in(text):
        with prompt_lock:
            prompt_seq+=1

def get_prompt_seq():
    with prompt_lock:
        return prompt_seq

def wait_for_next_prompt(prev_seq, timeout):
    if not SCRIPT_WAIT_PROMPT: return prev_seq
    deadline=time.time()+timeout
    while time.time()<deadline:
        cur=get_prompt_seq()
        if cur>prev_seq:
            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
            return cur
        time.sleep(0.01)
    return get_prompt_seq()

# ================== i2cdump capture ====================================
_i2c_capture_buffer_fragment=""
_i2c_capture_active=False
_i2c_capture_lines=[]
_last_captured_dump=None

_I2C_HEADER_RE = re.compile(r'^\s+00(?:\s+[0-9A-Fa-f]{2}){15}\s*$')
_I2C_DATA_ROW_RE = re.compile(r'^[0-9A-Fa-f]{2}:\s+([0-9A-Fa-f]{2}\s+){0,15}[0-9A-Fa-f]{2}\s*$')
_LAST_ADDR = "f0"

def _maybe_finalize_partial(reason:str):
    global _i2c_capture_active,_i2c_capture_lines,_last_captured_dump
    if _i2c_capture_active and _i2c_capture_lines:
        _last_captured_dump=_i2c_capture_lines[:]
        print(f"\n[dumps] captured ({reason}) {len(_last_captured_dump)} lines")
    _i2c_capture_active=False
    _i2c_capture_lines=[]

def _i2c_capture_feed(chunk:str):
    global _i2c_capture_buffer_fragment,_i2c_capture_active,_i2c_capture_lines,_last_captured_dump
    if not chunk: return
    _i2c_capture_buffer_fragment += chunk
    while True:
        if '\n' not in _i2c_capture_buffer_fragment:
            break
        line,rest=_i2c_capture_buffer_fragment.split('\n',1)
        _i2c_capture_buffer_fragment=rest
        line=line.rstrip('\r')
        if PROMPT_PATTERNS and _line_is_prompt_start(line):
            if _i2c_capture_active:
                _maybe_finalize_partial("prompt")
            continue
        if not _i2c_capture_active:
            if _I2C_HEADER_RE.match(line):
                _i2c_capture_active=True
                _i2c_capture_lines=[line]
                continue
            if re.match(r'^00:\s', line):
                _i2c_capture_active=True
                _i2c_capture_lines=["#NO_HEADER#"]
            else:
                continue
        if _i2c_capture_active:
            if _I2C_DATA_ROW_RE.match(line) or line==_i2c_capture_lines[0]:
                if line != _i2c_capture_lines[0]:
                    _i2c_capture_lines.append(line)
            else:
                if line.strip():
                    _i2c_capture_lines.append(line)
            if line.lower().startswith(_LAST_ADDR + ":"):
                _last_captured_dump=_i2c_capture_lines[:]
                print(f"\n[dumps] captured i2cdump ({len(_last_captured_dump)} lines)")
                _i2c_capture_active=False
                _i2c_capture_lines=[]
                continue
            if len(_i2c_capture_lines) > 60:
                _maybe_finalize_partial("overflow")
                continue

# ================== Receiver thread (UNCHANGED) =======================
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser=ser; self.encoding=encoding
        self.hex_dump=hex_dump; self.raw=raw
        self.log_file=log_file; self.quiet=quiet
        self._running=True
    def stop(self): self._running=False
    def run(self):
        while self._running and self.ser.is_open:
            try:
                data=self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[err] serial exception: {e}")
                break
            if not data: continue
            if self.log_file:
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n"); self.log_file.flush()
                except Exception: pass
            if self.quiet: continue
            if self.hex_dump:
                txt=format_hex(data)
                print(f"[rx] {txt}")
                inc_prompt_if_in(txt)
                _i2c_capture_feed(txt+"\n")
            elif self.raw:
                sys.stdout.buffer.write(data); sys.stdout.flush()
                try:
                    decoded=data.decode(self.encoding,errors="ignore")
                    inc_prompt_if_in(decoded)
                    _i2c_capture_feed(decoded)
                except: pass
            else:
                try:
                    text=data.decode(self.encoding,errors="replace")
                except Exception:
                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
                print(text,end="",flush=True)
                inc_prompt_if_in(text)
                _i2c_capture_feed(text)

# ================== Port selection =====================================
def load_last_port():
    if not REMEMBER_LAST: return None
    try:
        if os.path.isfile(LAST_FILE_NAME):
            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
                v=f.read().strip()
                if v: return v
    except: pass
    return None

def save_last_port(p):
    if not REMEMBER_LAST: return
    try:
        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
            f.write(p.strip())
    except: pass

def interactive_select_port(default_port):
    port=default_port; baud=BAUD; parity_name=PARITY_NAME
    data_bits=DATA_BITS; stop_bits=STOP_BITS; flow_ctrl=FLOW_CTRL; enter_mode=ENTER_MODE
    last=load_last_port()
    if last: default_port=last
    if not INTERACTIVE_SELECT:
        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
    print(ui_head("serial interactive config"))
    if list_ports:
        ports=list(list_ports.comports())
        if ports:
            for idx,p in enumerate(ports,1):
                print(f" {idx}. {p.device:<8} {p.description} ({p.hwid})")
        else:
            print(" (no detected ports)")
    val=input(f"port [{default_port}]: ").strip()
    if val: port=val
    val=input(f"baud [{baud}]: ").strip()
    if val.isdigit(): baud=int(val)
    plist=["none","even","odd","mark","space"]
    val=input(f"parity {plist} [{parity_name}]: ").strip().lower()
    if val in plist: parity_name=val
    val=input(f"data bits (7/8) [{data_bits}]: ").strip()
    if val in ("7","8"): data_bits=int(val)
    val=input(f"stop bits (1/2) [{STOP_BITS}]: ").strip()
    if val in ("1","2"): stop_bits=int(val)
    flist=["none","rtscts","dsrdtr","x"]
    val=input(f"flowctrl {flist} [{flow_ctrl}]: ").strip().lower()
    if val in flist: flow_ctrl=val
    emlist=["CR","CRLF","LF","NONE"]
    val=input(f"enter mode {emlist} [{enter_mode}]: ").strip().upper()
    if val in emlist: enter_mode=val
    save_last_port(port)
    print(ui_line())
    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode

# ================== Hotkey Thread ======================================
class HotkeyThread(threading.Thread):
    def __init__(self,
                 play_callback,
                 show_all_callback,
                 combo_list_callback,
                 run_single_combo_callback,
                 stop_event):
        super().__init__(daemon=True)
        self.play_callback=play_callback
        self.show_all_callback=show_all_callback
        self.combo_list_callback=combo_list_callback
        self.run_single_combo_callback=run_single_combo_callback
        self.stop_event=stop_event
        import ctypes
        self.ctypes=ctypes
        self.user32=ctypes.WinDLL("user32", use_last_error=True)
        self.VK_CTRL=0x11; self.VK_S=0x53
        self.VK_C=0x43; self.VK_B=0x42; self.VK_L=0x4C
        self.VK_0_9=list(range(0x30,0x3A))
        self.VK_NUM_0_9=list(range(0x60,0x6A))
        self.VK_A_Z=list(range(0x41,0x5B))
        self.prev_digit_down={vk:False for vk in self.VK_0_9+self.VK_NUM_0_9}
        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
        self.prev_s_down=False
        self.prev_cl_combo_list=False
    def key_down(self,vk):
        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
    def run(self):
        while not self.stop_event.is_set():
            ctrl=self.key_down(self.VK_CTRL)
            s_now=ctrl and self.key_down(self.VK_S)
            if s_now and not self.prev_s_down:
                print(); self.show_all_callback()
            self.prev_s_down=s_now
            c_now=self.key_down(self.VK_C); b_now=self.key_down(self.VK_B)
            cb_now=c_now and b_now
            if cb_now:
                for vk in self.VK_0_9+self.VK_NUM_0_9:
                    now=self.key_down(vk)
                    if now and not self.prev_digit_down[vk]:
                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
                        self.run_single_combo_callback(key)
                    self.prev_digit_down[vk]=now
            else:
                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
            l_now=self.key_down(self.VK_L); cl_now=c_now and l_now
            if cl_now and not self.prev_cl_combo_list:
                print(); self.combo_list_callback()
            self.prev_cl_combo_list=cl_now
            if ctrl:
                for vk in self.VK_0_9+self.VK_NUM_0_9:
                    now=self.key_down(vk)
                    if now and not self.prev_digit_down[vk]:
                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
                        print(); self.play_callback(key.lower())
                    self.prev_digit_down[vk]=now
                for vk in self.VK_A_Z:
                    now=self.key_down(vk)
                    if now and not self.prev_letter_down[vk]:
                        key=chr(vk).lower()
                        print(); self.play_callback(key)
                    self.prev_letter_down[vk]=now
            else:
                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
                self.prev_s_down=False
            time.sleep(HOTKEY_POLL_INTERVAL_SEC)

# ================== Main ================================================
def main():
    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO
    global SCRIPT_PROMPT_TIMEOUT_SEC, SCRIPT_WAIT_PROMPT, FAST_PLAY_MODE
    global PROMPT_PATTERNS
    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
    user_cfg=load_user_config()

    # restore
    for k in ("char_delay_ms","line_delay_ms","script_char_delay_ms","prompt_timeout_sec"):
        if k in user_cfg:
            try:
                v=float(user_cfg[k])
                if v>=0:
                    if k=="char_delay_ms": globals()['CHAR_DELAY_MS']=v
                    elif k=="line_delay_ms": globals()['LINE_DELAY_MS']=v
                    elif k=="script_char_delay_ms": SAFE_SCRIPT_CHAR_DELAY_MS=v
                    elif k=="prompt_timeout_sec": SCRIPT_PROMPT_TIMEOUT_SEC=v
            except: pass
    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
    if "script_local_echo" in user_cfg: SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
    if "script_wait_prompt" in user_cfg: SCRIPT_WAIT_PROMPT=bool(user_cfg["script_wait_prompt"])
    if "fast_play_mode" in user_cfg: FAST_PLAY_MODE=bool(user_cfg["fast_play_mode"])
    if "prompt_patterns" in user_cfg:
        pp=user_cfg["prompt_patterns"]
        if isinstance(pp,list):
            cleaned=[str(p) for p in pp if isinstance(p,str) and p.strip()]
            if cleaned: PROMPT_PATTERNS=cleaned[:]

    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
    init_baud=cfg_ini.get("BaudRate",BAUD)
    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
    if init_enter not in ("CR","CRLF","LF","NONE"): init_enter="CR"

    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)

    parity_map={"even":serial.PARITY_EVEN,"odd":serial.PARITY_ODD,"none":serial.PARITY_NONE,
                "mark":serial.PARITY_MARK,"space":serial.PARITY_SPACE}
    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE

    if fc in ("rtscts","hard"):
        rtscts,dsrdtr,xonxoff=True,False,False
    elif fc=="dsrdtr":
        rtscts,dsrdtr,xonxoff=False,True,False
    elif fc=="x":
        rtscts,dsrdtr,xonxoff=False,False,True
    else:
        rtscts=dsrdtr=xonxoff=False

    char_delay=float(globals()['CHAR_DELAY_MS']); line_delay=float(globals()['LINE_DELAY_MS'])

    try:
        ser=serial.Serial(port,baud,timeout=TIMEOUT,
                          bytesize=bytesize,parity=parity,stopbits=stopbits,
                          rtscts=rtscts,dsrdtr=dsrdtr,xonxoff=xonxoff,write_timeout=1)
    except serial.SerialException as e:
        print(f"[err] cannot open {port}: {e}"); return

    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[warn] set dtr/rts failed: {e}")

    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
        try:
            ser.reset_input_buffer(); ser.reset_output_buffer()
        except Exception as e: print(f"[warn] clear buffers failed: {e}")

    if not NO_BANNER:
        patt_summary=" | ".join(PROMPT_PATTERNS)
        lines=[
            ui_kv("port",ser.port),
            ui_kv("baud",ser.baudrate),
            ui_kv("data/parity/stop",f"{data_bits}/{parity_name}/{stop_bits_val}"),
            ui_kv("flow","rtscts="+str(rtscts)+" dsrdtr="+str(dsrdtr)+" xonxoff="+str(xonxoff)),
            ui_kv("enter",enter_mode),
            ui_kv("char_delay",f"{char_delay} ms"),
            ui_kv("line_delay",f"{line_delay} ms"),
            ui_kv("script_char_min",f"{SAFE_SCRIPT_CHAR_DELAY_MS} ms"),
            ui_kv("hex_tx","on" if TX_HEX else "off"),
            ui_kv("script_echo","on" if SCRIPT_LOCAL_ECHO else "off"),
            ui_kv("prompt_wait","on" if SCRIPT_WAIT_PROMPT else "off"),
            ui_kv("prompt_timeout",f"{SCRIPT_PROMPT_TIMEOUT_SEC}s"),
            ui_kv("fastplay","on" if FAST_PLAY_MODE else "off"),
            ui_kv("prompts",patt_summary)
        ]
        ui_print_block("session", lines)
        print("[info] type /help for commands")

    log_file=None
    if LOG_PATH:
        try:
            log_file=open(LOG_PATH,"a",encoding="utf-8")
            print(f"[info] logging -> {LOG_PATH}")
        except Exception as e:
            print(f"[warn] log open failed: {e}")

    reader=SerialReaderThread(
        ser,encoding=ENCODING,hex_dump=HEX_DUMP_RX,raw=RAW_RX,
        log_file=log_file,quiet=QUIET_RX
    )
    reader.start()
    send_lock=threading.Lock()

    def persist_user():
        user_cfg={
            "char_delay_ms":char_delay,
            "line_delay_ms":line_delay,
            "tx_hex":TX_HEX,
            "script_char_delay_ms":SAFE_SCRIPT_CHAR_DELAY_MS,
            "script_local_echo":SCRIPT_LOCAL_ECHO,
            "script_wait_prompt":SCRIPT_WAIT_PROMPT,
            "prompt_timeout_sec":SCRIPT_PROMPT_TIMEOUT_SEC,
            "fast_play_mode":FAST_PLAY_MODE,
            "prompt_patterns":PROMPT_PATTERNS
        }
        save_user_config(user_cfg)

    def line_suffix():
        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]

    def send_bytes(data:bytes, tag="tx", safe=False, local_echo_line=None):
        if not data: return
        per_char_delay=char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
            print(local_echo_line)
        if per_char_delay>0 and len(data)>1:
            for i,b in enumerate(data):
                with send_lock:
                    try: ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[err] tx failed: {e}"); return
                if TX_HEX and not QUIET_RX: print(f"[{tag}] {format_hex(bytes([b]))}")
                if log_file:
                    ts=datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    try: log_file.write(f"[{ts}] {tag.upper()} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except: pass
                if i<len(data)-1: time.sleep(per_char_delay/1000.0)
        else:
            with send_lock:
                try: ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[err] tx failed: {e}"); return
            if TX_HEX and not QUIET_RX: print(f"[{tag}] {format_hex(data)}")
            if log_file:
                ts=datetime.now().strftime("%H:%M:%S.%f")[:-3]
                try: log_file.write(f"[{ts}] {tag.upper()} {format_hex(data)}\n"); log_file.flush()
                except: pass
        if line_delay>0 and tag=="tx": time.sleep(line_delay/1000.0)

    class ScriptContext:
        def __init__(self):
            self.last_prompt_seq=get_prompt_seq()
            self.first_send=True
        def wait_ready_if_needed(self):
            if FAST_PLAY_MODE: return
            if not SCRIPT_WAIT_PROMPT: return
            if self.first_send:
                self.first_send=False; return
            prev=self.last_prompt_seq
            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
        def note_after_send(self): pass

    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
        if safe and script_ctx: script_ctx.wait_ready_if_needed()
        try: body=text.encode(ENCODING,errors="replace")
        except Exception as e: print(f"[warn] encode failed: {e}"); return
        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
        if safe and script_ctx: script_ctx.note_after_send()

    def send_enter_only(safe=False, script_ctx=None):
        if safe and script_ctx: script_ctx.wait_ready_if_needed()
        send_bytes(line_suffix(), tag="tx-empty", safe=safe)
        if safe and script_ctx: script_ctx.note_after_send()

    # Data holders
    slot_cmds={k:None for k in ALL_SLOTS}; load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
    global_combos={}; load_global_combos(COMBO_SAVE_FILE, global_combos)
    i2c_dump_slots={str(i):None for i in range(MAX_I2C_DUMPS)}; load_i2c_dumps(I2C_DUMP_SAVE_FILE, i2c_dump_slots)
    cmp_history=load_cmp_history(DUMPCMP_HISTORY_FILE)
    load_cmp_results(DUMPCMP_RESULTS_FILE)

    # Display helpers
    def show_slots():
        rows=[]
        for k in DIGIT_SLOTS+LETTER_SLOTS:
            v=slot_cmds.get(k)
            if v is None:
                rows.append(f"{k}: (empty)")
            else:
                t=v.get("type")
                if t=="enter":
                    rows.append(f"{k}: <enter>")
                elif t=="combo":
                    rows.append(f"{k}: <combo {v.get('seq','')}>")
                else:
                    d=v.get("data","")
                    one=d.splitlines()[0] if d else ""
                    more="…" if "\n" in d else ""
                    rows.append(f"{k}: {one[:50]}{more}")
        ui_print_block("slots", rows)

    def show_global_combos():
        rows=[]
        for d in DIGIT_SLOTS:
            if d in global_combos:
                rows.append(f"{d}: {global_combos[d]}")
            else:
                rows.append(f"{d}: (empty)")
        ui_print_block("digit combos", rows)

    def dumplist():
        rows=[f"{d}: {(str(len(v))+' lines') if v else '(empty)'}" for d,v in i2c_dump_slots.items()]
        ui_print_block("i2c dump slots", rows)

    def dump_show(d):
        v=i2c_dump_slots.get(d)
        if not v:
            print(f"[dumps] slot {d} empty"); return
        print(ui_head(f"dump slot {d} ({len(v)} lines)"))
        for ln in v:
            print(ln)
        print(ui_line())

    HEADER_LINE = "     " + " ".join(f"{i:02x}" for i in range(16))
    ROW_ADDRS   = [f"{i:02x}" for i in range(0,256,16)]

    def _parse_dump_to_matrix(lines):
        matrix={}
        for ln in lines:
            if ln.startswith("#NO_HEADER#"):
                continue
            m=re.match(r'^([0-9A-Fa-f]{2}):\s+(.*)$', ln)
            if not m: continue
            addr=m.group(1).lower()
            rest=m.group(2).strip()
            bytes_list=[b for b in rest.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', b)]
            if len(bytes_list)<16: bytes_list+=["--"]*(16-len(bytes_list))
            elif len(bytes_list)>16: bytes_list=bytes_list[:16]
            matrix[addr]=[b.upper() for b in bytes_list]
        for a in ROW_ADDRS:
            if a not in matrix: matrix[a]=["--"]*16
        return matrix

    def _hex_to_bin(h):
        try:
            bits=f"{int(h,16):08b}"
            return bits[:4]+"_"+bits[4:]
        except:
            return "----_----"

    def _store_cmp_result(entry):
        _dumpcmp_results.append(entry)
        if len(_dumpcmp_results)>MAX_CMP_RESULTS_ENTRIES:
            del _dumpcmp_results[:-MAX_CMP_RESULTS_ENTRIES]
        save_cmp_results(DUMPCMP_RESULTS_FILE)

    def _dump_compare_single(a,b,*,suppress_end=False):
        da=i2c_dump_slots.get(a); db=i2c_dump_slots.get(b)
        if not da: print(f"[dumpcmp] slot {a} empty"); return None
        if not db: print(f"[dumpcmp] slot {b} empty"); return None
        mA=_parse_dump_to_matrix(da); mB=_parse_dump_to_matrix(db)
        changed_bytes=0; changed_rows=0
        hex_lines=[]; bin_lines=[]
        print("hex"); hex_lines.append("hex")
        print(f" disk:{a}"); hex_lines.append(f" disk:{a}")
        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
        for addr in ROW_ADDRS:
            rowA=mA[addr]; rowB=mB[addr]; row_tokens=[]; row_changed=False
            for i in range(16):
                if rowA[i]==rowB[i]:
                    row_tokens.append("XX")
                else:
                    row_tokens.append(rowA[i]); changed_bytes+=1; row_changed=True
            if row_changed: changed_rows+=1
            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); hex_lines.append(ln)
        print(f"disk:{b}"); hex_lines.append(f"disk:{b}")
        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
        for addr in ROW_ADDRS:
            rowA=mA[addr]; rowB=mB[addr]
            row_tokens=["XX" if rowA[i]==rowB[i] else rowB[i] for i in range(16)]
            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); hex_lines.append(ln)
        print(); bin_lines.append("")
        print("binary"); bin_lines.append("binary")
        print(f"disk:{a}"); bin_lines.append(f"disk:{a}")
        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
        for addr in ROW_ADDRS:
            rowA=mA[addr]; rowB=mB[addr]
            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowA[i]) for i in range(16)]
            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); bin_lines.append(ln)
        print(f"disk:{b}"); bin_lines.append(f"disk:{b}")
        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
        for addr in ROW_ADDRS:
            rowA=mA[addr]; rowB=mB[addr]
            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowB[i]) for i in range(16)]
            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); bin_lines.append(ln)
        if not suppress_end: print("[dumpcmp] end")
        entry={
            "timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "a":a,"b":b,
            "changed_rows":changed_rows,
            "changed_bytes":changed_bytes,
            "hex_lines":hex_lines,
            "binary_lines":bin_lines
        }
        _store_cmp_result(entry)
        return {"a":a,"b":b,"changed_rows":changed_rows,"changed_bytes":changed_bytes}

    def dump_compare(a,b):
        return _dump_compare_single(a,b)

    def parse_multi_pairs(arg_str):
        parts=[p.strip() for p in arg_str.split(",") if p.strip()]
        pairs=[]
        for p in parts:
            toks=p.split()
            if len(toks)!=2:
                print(f"[dumpcmp] skip invalid pair '{p}'"); continue
            da,db=toks
            if da in DIGIT_SLOTS and db in DIGIT_SLOTS:
                pairs.append((da,db))
            else:
                print(f"[dumpcmp] skip non-digit pair '{p}'")
        return pairs

    def multi_dump_compare(pairs):
        if not pairs:
            print("[dumpcmp] no valid pairs"); return
        session_stats=[]
        print(f"[dumpcmp] multi {len(pairs)} pair(s): {', '.join(f'{a}-{b}' for a,b in pairs)}")
        for idx,(a,b) in enumerate(pairs,1):
            print(f"\n[dumpcmp] pair {idx}/{len(pairs)} {a} vs {b}")
            stats=_dump_compare_single(a,b,suppress_end=True)
            if stats:
                print("[dumpcmp] end"); session_stats.append(stats)
        entry={
            "timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pairs":[{"a":s["a"],"b":s["b"],"changed_rows":s["changed_rows"],"changed_bytes":s["changed_bytes"]} for s in session_stats]
        }
        cmp_history.append(entry); save_cmp_history(DUMPCMP_HISTORY_FILE,cmp_history)
        total_changed=sum(s["changed_bytes"] for s in session_stats)
        print(f"\n[dumpcmp] summary: {len(session_stats)} compared  total_changed={total_changed}")
        for s in session_stats:
            print(f"  {s['a']}-{s['b']}: rows={s['changed_rows']} bytes={s['changed_bytes']}")

    def show_cmp_history(limit=10):
        tail=cmp_history[-limit:]
        print(ui_head("multi compare history"))
        if not tail: print(" (none)")
        else:
            for idx,entry in enumerate(tail,1):
                ts=entry.get("timestamp","?")
                pairs_txt=", ".join(f"{p['a']}-{p['b']}:{p['changed_bytes']}" for p in entry.get("pairs",[]))
                print(f"{idx:2}. {ts}  {pairs_txt}")
        print(ui_line())

    def show_cmp_results(limit=10):
        tail=_dumpcmp_results[-limit:]
        print(ui_head("stored pair results"))
        if not tail: print(" (none)")
        else:
            for i,entry in enumerate(tail,1):
                print(f"{i:2}. {entry.get('timestamp','?')} {entry.get('a')}-{entry.get('b')} "
                      f"bytes={entry.get('changed_bytes')} rows={entry.get('changed_rows')}")
        print(ui_line())

    def clear_cmp_results():
        global _dumpcmp_results
        _dumpcmp_results=[]
        save_cmp_results(DUMPCMP_RESULTS_FILE)
        print("[cmpres] cleared")

    def overview():
        show_slots(); show_global_combos(); dumplist()

    def print_help():
        print(ui_head("help"))
        print("""groups:
  slots          : /setx /combox /enterx /clrx ox /slots /slotsave /slotload
  combos (digit) : /cset d <seq> /clist /crun d /cclear d /crun_all /csave /cload
  i2cdump        : /dumpsave d /dumpshow d /dumplist
                   /dumpcmp a b
                   /dumpcmp a b,c d,e f ... OR /dumpcmpmulti ...
  compare meta   : /cmphist [n] /cmpres [n] /cmpresclear
  timing         : /delay /scriptdelay /linedelay
  script flow    : /fastplay on|off /scriptwait on|off /promptime [sec]
  mode toggles   : /hex on|off /scriptecho on|off
  general        : /help /quit

notes:
  - all commands lowercase
  - fastplay skips ALL prompt waits
  - prompt patterns editable in .console_config.json (key: prompt_patterns)
""")
        print(ui_line())

    # slot execution
    def play_slot_recursive(idx_char, depth, visited, script_ctx):
        if depth>40:
            print("[play] depth limit"); return
        if idx_char not in slot_cmds:
            print(f"[play] slot {idx_char} not found"); return
        v=slot_cmds[idx_char]
        if v is None:
            print(f"[play] slot {idx_char} empty"); return
        if id(v) in visited:
            print(f"[play] cycle at {idx_char}"); return
        visited.add(id(v))
        t=v.get("type")
        if t=="enter":
            send_enter_only(safe=True, script_ctx=script_ctx)
        elif t=="combo":
            for c in v.get("seq",""):
                if c in slot_cmds:
                    play_slot_recursive(c, depth+1, visited, script_ctx)
        else:
            data=v.get("data","")
            parts=data.split(TOKEN_ENTER)
            for pi,segment in enumerate(parts):
                lines=segment.splitlines()
                if not lines and segment=="":
                    send_enter_only(safe=True, script_ctx=script_ctx)
                for line in lines:
                    if line.strip()=="" and line!="":
                        send_enter_only(safe=True, script_ctx=script_ctx)
                    elif line!="":
                        send_line(line,safe=True,
                                  local_echo=f"[run] {line}" if SCRIPT_LOCAL_ECHO else None,
                                  script_ctx=script_ctx)
                if pi<len(parts)-1:
                    send_enter_only(safe=True, script_ctx=script_ctx)
        visited.remove(id(v))

    def play_slot(k):
        if k not in slot_cmds:
            print(f"[play] invalid slot {k}"); return
        print(f"[play] slot {k}")
        ctx=ScriptContext()
        play_slot_recursive(k,0,set(),ctx)

    def run_global_combo(d):
        if d not in global_combos:
            print(f"[combo] digit {d} undefined"); return
        seq=global_combos[d]; print(f"[combo] run {d}: {seq}")
        ctx=ScriptContext()
        for c in seq:
            if c in slot_cmds:
                play_slot_recursive(c,0,set(),ctx)

    def run_all_global_combos():
        defined=[d for d in DIGIT_SLOTS if d in global_combos]
        if not defined:
            print("[combo] none defined"); return
        print("[combo] run all:")
        ctx=ScriptContext()
        for d in defined:
            seq=global_combos[d]; print(f"  {d}: {seq}")
            for c in seq:
                if c in slot_cmds:
                    play_slot_recursive(c,0,set(),ctx)

    def run_single_combo_via_hotkey(d):
        if d in global_combos:
            print(f"[combo] (hotkey) {d}")
            run_global_combo(d)
        else:
            print(f"[combo] (hotkey) {d} undefined")

    # hotkeys
    stop_hotkey=threading.Event()
    hotkey_thread=None
    if os.name=='nt':
        try:
            hotkey_thread=HotkeyThread(
                play_callback=play_slot,
                show_all_callback=overview,
                combo_list_callback=show_global_combos,
                run_single_combo_callback=run_single_combo_via_hotkey,
                stop_event=stop_hotkey
            )
            hotkey_thread.start()
        except Exception as e:
            print(f"[warn] hotkey thread failed: {e}")

    # ================== Command loop ==================================
    try:
        while True:
            try:
                line=input()
            except EOFError:
                break
            stripped=line.strip()
            lower=stripped.lower()

            if lower=="/help":
                print_help(); continue

            # i2c dump commands
            if lower.startswith("/dumpsave"):
                parts=lower.split()
                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
                    print("[dumps] usage: /dumpsave <digit>")
                else:
                    d=parts[1]
                    if _last_captured_dump:
                        i2c_dump_slots[d]=_last_captured_dump[:]
                        print(f"[dumps] saved to slot {d} ({len(_last_captured_dump)} lines)")
                        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
                    else:
                        print("[dumps] no captured dump")
                continue

            if lower.startswith("/dumpshow"):
                parts=lower.split()
                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
                    print("[dumps] usage: /dumpshow <digit>")
                else:
                    dump_show(parts[1])
                continue

            if lower=="/dumplist":
                dumplist(); continue

            # dump compare
            if lower.startswith("/dumpcmp") or lower.startswith("/dumpcmpmulti"):
                cmd,*rest=lower.split(None,1)
                if not rest:
                    print("[dumpcmp] usage: /dumpcmp a b | /dumpcmp a b,c d,e f")
                    continue
                rem=rest[0].strip()
                if ',' in rem or len(rem.split())>2:
                    if ',' not in rem:
                        toks=rem.split()
                        if len(toks)>=4 and len(toks)%2==0:
                            rem=",".join(f"{toks[i]} {toks[i+1]}" for i in range(0,len(toks),2))
                    pairs=parse_multi_pairs(rem)
                    multi_dump_compare(pairs)
                else:
                    p=rem.split()
                    if len(p)!=2 or p[0] not in DIGIT_SLOTS or p[1] not in DIGIT_SLOTS:
                        print("[dumpcmp] usage: /dumpcmp <a> <b>")
                    else:
                        dump_compare(p[0],p[1])
                continue

            if lower.startswith("/cmphist"):
                parts=lower.split()
                lim=10
                if len(parts)==2 and parts[1].isdigit():
                    lim=max(1,min(100,int(parts[1])))
                show_cmp_history(lim)
                continue

            if lower.startswith("/cmpresclear"):
                confirm=input("confirm clear compare results (yes): ").strip().lower()
                if confirm=="yes":
                    clear_cmp_results()
                else:
                    print("[cmpres] cancelled")
                continue

            if lower.startswith("/cmpres"):
                parts=lower.split()
                lim=10
                if len(parts)==2 and parts[1].isdigit():
                    lim=max(1,min(200,int(parts[1])))
                show_cmp_results(lim)
                continue

            # timing / modes
            if lower.startswith("/delay"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[delay] {char_delay} ms")
                else:
                    try:
                        v=float(parts[1]); assert v>=0
                        char_delay=v; print(f"[delay] -> {char_delay} ms"); persist_user()
                    except: print("[delay] invalid")
                continue

            if lower.startswith("/scriptdelay"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[scriptdelay] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
                else:
                    try:
                        v=float(parts[1]); assert v>=0
                        SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[scriptdelay] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
                    except: print("[scriptdelay] invalid")
                continue

            if lower.startswith("/linedelay"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[linedelay] {line_delay} ms")
                else:
                    try:
                        v=float(parts[1]); assert v>=0
                        line_delay=v; print(f"[linedelay] -> {line_delay} ms"); persist_user()
                    except: print("[linedelay] invalid")
                continue

            if lower.startswith("/scriptecho"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[scriptecho] {'on' if SCRIPT_LOCAL_ECHO else 'off'}")
                else:
                    arg=parts[1]
                    if arg in ("on","off"):
                        SCRIPT_LOCAL_ECHO=(arg=="on")
                        print(f"[scriptecho] -> {'on' if SCRIPT_LOCAL_ECHO else 'off'}"); persist_user()
                    else:
                        print("[scriptecho] use /scriptecho on|off")
                continue

            if lower.startswith("/hex"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[hex] {'on' if TX_HEX else 'off'}")
                else:
                    arg=parts[1]
                    if arg in ("on","off"):
                        TX_HEX=(arg=="on")
                        print(f"[hex] -> {'on' if TX_HEX else 'off'}"); persist_user()
                    else:
                        print("[hex] use /hex on|off")
                continue

            if lower.startswith("/fastplay"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[fastplay] {'on' if FAST_PLAY_MODE else 'off'}")
                else:
                    arg=parts[1]
                    if arg in ("on","off"):
                        FAST_PLAY_MODE=(arg=="on")
                        print(f"[fastplay] -> {'on' if FAST_PLAY_MODE else 'off'}"); persist_user()
                    else:
                        print("[fastplay] use /fastplay on|off")
                continue

            if lower.startswith("/scriptwait"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[scriptwait] {'on' if SCRIPT_WAIT_PROMPT else 'off'}")
                else:
                    arg=parts[1]
                    if arg in ("on","off"):
                        SCRIPT_WAIT_PROMPT=(arg=="on")
                        print(f"[scriptwait] -> {'on' if SCRIPT_WAIT_PROMPT else 'off'}"); persist_user()
                    else:
                        print("[scriptwait] use /scriptwait on|off")
                continue

            if lower.startswith("/promptime"):
                parts=lower.split(None,1)
                if len(parts)==1:
                    print(f"[promptime] {SCRIPT_PROMPT_TIMEOUT_SEC} s")
                else:
                    try:
                        v=float(parts[1]); assert v>=0
                        SCRIPT_PROMPT_TIMEOUT_SEC=v; print(f"[promptime] -> {SCRIPT_PROMPT_TIMEOUT_SEC} s"); persist_user()
                    except: print("[promptime] invalid")
                continue

            # slot persistence
            if lower=="/slotsave":
                save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
            if lower=="/slotload":
                load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue

            # combos
            if lower=="/clist":
                show_global_combos(); continue
            if lower.startswith("/cset "):
                parts=stripped.split(None,2)
                if len(parts)<3:
                    print("[combo] usage: /cset <digit> <seq>")
                else:
                    digit=parts[1]
                    if not (digit.isdigit() and len(digit)==1):
                        print("[combo] digit must be 0-9")
                    else:
                        seq="".join(ch for ch in parts[2] if ch.isalnum())
                        global_combos[digit]=seq
                        print(f"[combo] {digit} = {seq}")
                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
                continue
            if lower.startswith("/crun "):
                digit=lower.split(None,1)[1]
                run_global_combo(digit); continue
            if lower.startswith("/cclear "):
                digit=lower.split(None,1)[1]
                if digit in global_combos:
                    del global_combos[digit]; print(f"[combo] cleared {digit}")
                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
                else:
                    print("[combo] not defined")
                continue
            if lower=="/csave":
                save_global_combos(COMBO_SAVE_FILE,global_combos); continue
            if lower=="/cload":
                load_global_combos(COMBO_SAVE_FILE,global_combos); continue
            if lower=="/crun_all":
                run_all_global_combos(); continue

            # general
            if lower=="/quit":
                print("[info] quitting")
                break
            if lower=="/slots":
                show_slots(); continue

            # slot definitions
            if lower.startswith("/enter") and len(lower)==7:
                key=lower[6]
                if key in slot_cmds:
                    slot_cmds[key]={"type":"enter"}
                    print(f"[set] slot {key} = <enter>")
                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
                continue
            if lower.startswith("/combo") and len(lower)>=7:
                key=lower[6]
                if key in slot_cmds:
                    parts=stripped.split(None,1)
                    seq=""
                    if len(parts)>1:
                        seq="".join(ch for ch in parts[1] if ch.isalnum())
                    slot_cmds[key]={"type":"combo","seq":seq}
                    print(f"[set] slot {key} = <combo {seq}>")
                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
                continue
            if lower.startswith("/set") and len(lower)>=5:
                key=lower[4]
                if key in slot_cmds:
                    parts=stripped.split(None,1)
                    data=parts[1] if len(parts)>1 else ""
                    slot_cmds[key]={"type":"raw","data":data}
                    print(f"[set] slot {key} len={len(data)}")
                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
                continue
            if lower.startswith("/clr") and len(lower)==5:
                key=lower[4]
                if key in slot_cmds:
                    slot_cmds[key]=None
                    print(f"[clr] slot {key} cleared")
                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
                continue

            # run slot alias: oX
            if len(lower)==2 and lower[0]=='o':
                key=lower[1]
                if key in slot_cmds:
                    play_slot(key)
                continue

            # blank line -> ENTER
            if line=="":
                send_enter_only(safe=False); continue

            # normal user input
            try:
                body=line.encode(ENCODING,errors="replace")
            except Exception as e:
                print(f"[warn] encode failed: {e}")
                continue
            send_bytes(body+line_suffix(), safe=False, tag="tx")

    except KeyboardInterrupt:
        print("\n[info] keyboardinterrupt")
    finally:
        persist_user()
        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
        save_cmp_history(DUMPCMP_HISTORY_FILE, cmp_history)
        save_cmp_results(DUMPCMP_RESULTS_FILE)
        if 'hotkey_thread' in locals() and hotkey_thread:
            stop_hotkey.set(); hotkey_thread.join(timeout=0.5)
        reader.stop()
        time.sleep(0.05)
        try: ser.close()
        except: pass
        if 'log_file' in locals() and log_file:
            try: log_file.close()
            except: pass
        print("[info] exit")

if __name__ == "__main__":
    main()
    


    



    """

請給我一週份的7個檔案讓我多讀程式
每天要一個.py檔案
讓我能夠多讀
然後裡面每一行要有註釋詳細講解
我的python語法偏弱
要多補充說明與舉例
還要有觀念講解

我是python新手

    """







    """

請幫我整理一下程式介面

這段程式接收資料盡量不要動
保持完整風格
請給我完整程式檔案
我不要指令 請給我直接就可以執行的程式碼


    """









    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
250904_0001_i2cdump_data_compare_pass
250904_0002_i2cdump_data_compare_binary_pass
250904_0003_i2cdump_data_multiple_compare_pass
250904_0004_i2cdump_data_multiple_compare_save_pass
250905_0001_prompt_timeout_save_pass
250905_0002_prompt_detect_UI_set_pass

    """

#    """
##!/usr/bin/env python
## -*- coding: utf-8 -*-
#"""
#Serial console (refined interface version)
#
#Key features (unchanged logic, cleaned interface):
# - Slots 0-9 + a-z
# - Digit global combos (0-9)
# - Hotkeys (Windows):
#     Ctrl+0..9 / Ctrl+a..z  : play slot
#     Ctrl+S                 : show overview
#     C+B+<digit>            : run single digit combo
#     C+L                    : list digit combos
# - i2cdump capture & storage (/dumpsave /dumpshow /dumplist)
# - Diff compare: /dumpcmp (single or multi), history & stored pair results
# - Multi compare history (.dumpcmp_history.json)
# - Stored pair results (.dumpcmp_results.json)
# - Multiple prompt detection via .console_config.json:
#       "prompt_patterns": ["i2c>", "~ #", "# "]
# - Script flow control:
#     /fastplay on|off
#     /scriptwait on|off
#     /promptime <sec>
# - Timing:
#     /delay /scriptdelay /linedelay
# - Output tightening & consistent lowercase tags
# - Receiver thread logic preserved (unchanged)
# - Interface cleaned: aligned sections, concise prefixes, improved /help
#
#Config persistence keys in .console_config.json:
#  char_delay_ms, line_delay_ms, tx_hex, script_char_delay_ms, script_local_echo,
#  script_wait_prompt, prompt_timeout_sec, fast_play_mode, prompt_patterns
#
#You requested: keep receiver logic, keep style spirit, only tidy interface.
#"""
#
#import sys
#import serial
#import threading
#import time
#import os
#import json
#import re
#from datetime import datetime
#
#try:
#    from serial.tools import list_ports
#except ImportError:
#    list_ports = None
#
## ================== Runtime / persisted config defaults ==================
#PORT                    = "COM5"
#BAUD                    = 115200
#PARITY_NAME             = "none"
#DATA_BITS               = 8
#STOP_BITS               = 1
#FLOW_CTRL               = "none"
#ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
#ENCODING                = "utf-8"
#TIMEOUT                 = 0.05
#CHAR_DELAY_MS           = 0
#LINE_DELAY_MS           = 0
#ASSERT_DTR              = False
#ASSERT_RTS              = False
#CLEAR_BUFF_ON_OPEN      = False
#
#TX_HEX                  = True
#HEX_DUMP_RX             = False
#RAW_RX                  = False
#QUIET_RX                = False
#
#LOG_PATH                = None
#INI_PATH                = None
#NO_BANNER               = False
#
#INTERACTIVE_SELECT      = True
#REMEMBER_LAST           = True
#LAST_FILE_NAME          = ".last_port"
#
#SLOTS_SAVE_FILE         = ".slot_cmds.json"
#AUTO_SAVE_SLOTS         = True
#SHOW_SAVE_MESSAGE       = False
#
#COMBO_SAVE_FILE         = ".combo_defs.json"
#AUTO_SAVE_COMBOS        = True
#SHOW_COMBO_SAVE_MSG     = False
#
#USER_CONFIG_FILE        = ".console_config.json"
#AUTO_SAVE_CONFIG        = True
#
#I2C_DUMP_SAVE_FILE      = ".i2c_dumps.json"
#AUTO_SAVE_I2C_DUMPS     = True
#MAX_I2C_DUMPS           = 10   # 0-9
#
#SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
#SCRIPT_LOCAL_ECHO         = False
#
#PROMPT_PATTERN            = "i2c>"
#PROMPT_PATTERNS           = ["i2c>"]     # replaced by list if config provides
#
#SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
#SCRIPT_WAIT_PROMPT        = True
#POST_PROMPT_STABILIZE_MS  = 5
#
#HOTKEY_POLL_INTERVAL_SEC  = 0.05
#TOKEN_ENTER               = "<ENTER>"
#
#DIGIT_SLOTS  = [str(i) for i in range(10)]
#LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
#ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS
#
#DUMPCMP_HISTORY_FILE     = ".dumpcmp_history.json"
#MAX_CMP_HISTORY_ENTRIES  = 200
#
#DUMPCMP_RESULTS_FILE     = ".dumpcmp_results.json"
#MAX_CMP_RESULTS_ENTRIES  = 400
#_dumpcmp_results         = []
#
#FAST_PLAY_MODE           = False
#
## UI formatting helpers =================================================
#def ui_line(char="-", width=66):
#    return char * width
#
#def ui_head(title):
#    t = f" {title} "
#    w = 66
#    if len(t) >= w-2:
#        return t
#    side = (w - len(t)) // 2
#    return f"{'-'*side}{t}{'-'*(w-len(t)-side)}"
#
#def ui_kv(label, value, pad=14):
#    return f"{label.rjust(pad)} : {value}"
#
#def ui_print_block(title, lines):
#    print(ui_head(title))
#    for ln in lines:
#        print(ln)
#    print(ui_line())
#
## ================== Utility ============================================
#def format_hex(data: bytes) -> str:
#    return " ".join(f"{b:02X}" for b in data)
#
#def parse_ini(path: str):
#    out = {}
#    if not path or not os.path.isfile(path):
#        return out
#    try:
#        with open(path, "r", encoding="utf-8", errors="ignore") as f:
#            for line in f:
#                line=line.strip()
#                if not line or line.startswith(";") or "=" not in line:
#                    continue
#                k,v=line.split("=",1)
#                k=k.strip(); v=v.strip()
#                kl=k.lower()
#                if kl in ("comport","baudrate","delayperchar","delayperline"):
#                    try: out[k]=int(v)
#                    except: pass
#                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
#                    out[k]=v
#    except Exception as e:
#        print(f"[warn] ini parse failed: {e}")
#    return out
#
#def load_user_config():
#    if not os.path.isfile(USER_CONFIG_FILE):
#        return {}
#    try:
#        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        return data if isinstance(data,dict) else {}
#    except Exception as e:
#        print(f"[cfg] load failed: {e}")
#        return {}
#
#def save_user_config(cfg):
#    if not AUTO_SAVE_CONFIG: return
#    try:
#        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
#            json.dump(cfg,f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[cfg] save failed: {e}")
#
#def normalize_slot_value(v):
#    if v is None: return None
#    if isinstance(v,dict):
#        t=v.get("type")
#        if t=="raw":
#            d=v.get("data"); return {"type":"raw","data": d if isinstance(d,str) else ""}
#        if t=="enter": return {"type":"enter"}
#        if t=="combo":
#            seq=v.get("seq","")
#            if not isinstance(seq,str): seq=""
#            return {"type":"combo","seq":seq}
#        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
#    if isinstance(v,str): return {"type":"raw","data":v}
#    return {"type":"raw","data":str(v)}
#
#def load_slots_from_file(path, slot_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        changed=False
#        for k in slot_dict.keys():
#            if k in data:
#                slot_dict[k]=normalize_slot_value(data[k]); changed=True
#        if changed: print(f"[slots] loaded {path}")
#    except Exception as e:
#        print(f"[slots] load failed: {e}")
#
#def save_slots_to_file(path, slot_dict):
#    try:
#        out={k:(None if v is None else v) for k,v in slot_dict.items()}
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(out,f,ensure_ascii=False,indent=2)
#        if SHOW_SAVE_MESSAGE:
#            print(f"[slots] saved -> {path}")
#    except Exception as e:
#        print(f"[slots] save failed: {e}")
#
#def load_global_combos(path, combo_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
#                    combo_dict[k]="".join(ch for ch in v if ch.isalnum())
#            print(f"[combo] loaded {path} ({len(combo_dict)} items)")
#    except Exception as e:
#        print(f"[combo] load failed: {e}")
#
#def save_global_combos(path, combo_dict):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
#        if SHOW_COMBO_SAVE_MSG:
#            print(f"[combo] saved -> {path}")
#    except Exception as e:
#        print(f"[combo] save failed: {e}")
#
#def load_i2c_dumps(path, dump_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if k in dump_dict and isinstance(v,list):
#                    dump_dict[k]=v
#        print(f"[dumps] loaded {path}")
#    except Exception as e:
#        print(f"[dumps] load failed: {e}")
#
#def save_i2c_dumps(path, dump_dict):
#    if not AUTO_SAVE_I2C_DUMPS: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(dump_dict,f,ensure_ascii=False,indent=2)
#        print(f"[dumps] saved -> {path}")
#    except Exception as e:
#        print(f"[dumps] save failed: {e}")
#
#def load_cmp_history(path):
#    if not os.path.isfile(path): return []
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,list): return data
#    except Exception as e:
#        print(f"[cmphist] load failed: {e}")
#    return []
#
#def save_cmp_history(path, hist):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(hist[-MAX_CMP_HISTORY_ENTRIES:],f,ensure_ascii=False,indent=2)
#        print(f"[cmphist] saved -> {path}")
#    except Exception as e:
#        print(f"[cmphist] save failed: {e}")
#
#def load_cmp_results(path):
#    global _dumpcmp_results
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,list):
#            _dumpcmp_results=data
#            print(f"[dumpcmp] loaded {len(_dumpcmp_results)} stored results")
#    except Exception as e:
#        print(f"[dumpcmp] results load failed: {e}")
#
#def save_cmp_results(path):
#    if not _dumpcmp_results: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(_dumpcmp_results[-MAX_CMP_RESULTS_ENTRIES:],f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[dumpcmp] results save failed: {e}")
#
## ================== Prompt tracking ====================================
#prompt_lock=threading.Lock()
#prompt_seq=0
#
#def _any_prompt_in(text:str)->bool:
#    for p in PROMPT_PATTERNS:
#        if p and p in text:
#            return True
#    return False
#
#def _line_is_prompt_start(line:str)->bool:
#    for p in PROMPT_PATTERNS:
#        if p and line.startswith(p):
#            return True
#    return False
#
#def inc_prompt_if_in(text:str):
#    global prompt_seq
#    if PROMPT_PATTERNS and _any_prompt_in(text):
#        with prompt_lock:
#            prompt_seq+=1
#
#def get_prompt_seq():
#    with prompt_lock:
#        return prompt_seq
#
#def wait_for_next_prompt(prev_seq, timeout):
#    if not SCRIPT_WAIT_PROMPT: return prev_seq
#    deadline=time.time()+timeout
#    while time.time()<deadline:
#        cur=get_prompt_seq()
#        if cur>prev_seq:
#            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
#            return cur
#        time.sleep(0.01)
#    return get_prompt_seq()
#
## ================== i2cdump capture ====================================
#_i2c_capture_buffer_fragment=""
#_i2c_capture_active=False
#_i2c_capture_lines=[]
#_last_captured_dump=None
#
#_I2C_HEADER_RE = re.compile(r'^\s+00(?:\s+[0-9A-Fa-f]{2}){15}\s*$')
#_I2C_DATA_ROW_RE = re.compile(r'^[0-9A-Fa-f]{2}:\s+([0-9A-Fa-f]{2}\s+){0,15}[0-9A-Fa-f]{2}\s*$')
#_LAST_ADDR = "f0"
#
#def _maybe_finalize_partial(reason:str):
#    global _i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if _i2c_capture_active and _i2c_capture_lines:
#        _last_captured_dump=_i2c_capture_lines[:]
#        print(f"\n[dumps] captured ({reason}) {len(_last_captured_dump)} lines")
#    _i2c_capture_active=False
#    _i2c_capture_lines=[]
#
#def _i2c_capture_feed(chunk:str):
#    global _i2c_capture_buffer_fragment,_i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if not chunk: return
#    _i2c_capture_buffer_fragment += chunk
#    while True:
#        if '\n' not in _i2c_capture_buffer_fragment:
#            break
#        line,rest=_i2c_capture_buffer_fragment.split('\n',1)
#        _i2c_capture_buffer_fragment=rest
#        line=line.rstrip('\r')
#        if PROMPT_PATTERNS and _line_is_prompt_start(line):
#            if _i2c_capture_active:
#                _maybe_finalize_partial("prompt")
#            continue
#        if not _i2c_capture_active:
#            if _I2C_HEADER_RE.match(line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=[line]
#                continue
#            if re.match(r'^00:\s', line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=["#NO_HEADER#"]
#            else:
#                continue
#        if _i2c_capture_active:
#            if _I2C_DATA_ROW_RE.match(line) or line==_i2c_capture_lines[0]:
#                if line != _i2c_capture_lines[0]:
#                    _i2c_capture_lines.append(line)
#            else:
#                if line.strip():
#                    _i2c_capture_lines.append(line)
#            if line.lower().startswith(_LAST_ADDR + ":"):
#                _last_captured_dump=_i2c_capture_lines[:]
#                print(f"\n[dumps] captured i2cdump ({len(_last_captured_dump)} lines)")
#                _i2c_capture_active=False
#                _i2c_capture_lines=[]
#                continue
#            if len(_i2c_capture_lines) > 60:
#                _maybe_finalize_partial("overflow")
#                continue
#
## ================== Receiver thread (UNCHANGED) =======================
#class SerialReaderThread(threading.Thread):
#    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
#        super().__init__(daemon=True)
#        self.ser=ser; self.encoding=encoding
#        self.hex_dump=hex_dump; self.raw=raw
#        self.log_file=log_file; self.quiet=quiet
#        self._running=True
#    def stop(self): self._running=False
#    def run(self):
#        while self._running and self.ser.is_open:
#            try:
#                data=self.ser.read(self.ser.in_waiting or 1)
#            except serial.SerialException as e:
#                print(f"[err] serial exception: {e}")
#                break
#            if not data: continue
#            if self.log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try:
#                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n"); self.log_file.flush()
#                except Exception: pass
#            if self.quiet: continue
#            if self.hex_dump:
#                txt=format_hex(data)
#                print(f"[rx] {txt}")
#                inc_prompt_if_in(txt)
#                _i2c_capture_feed(txt+"\n")
#            elif self.raw:
#                sys.stdout.buffer.write(data); sys.stdout.flush()
#                try:
#                    decoded=data.decode(self.encoding,errors="ignore")
#                    inc_prompt_if_in(decoded)
#                    _i2c_capture_feed(decoded)
#                except: pass
#            else:
#                try:
#                    text=data.decode(self.encoding,errors="replace")
#                except Exception:
#                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
#                print(text,end="",flush=True)
#                inc_prompt_if_in(text)
#                _i2c_capture_feed(text)
#
## ================== Port selection =====================================
#def load_last_port():
#    if not REMEMBER_LAST: return None
#    try:
#        if os.path.isfile(LAST_FILE_NAME):
#            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
#                v=f.read().strip()
#                if v: return v
#    except: pass
#    return None
#
#def save_last_port(p):
#    if not REMEMBER_LAST: return
#    try:
#        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
#            f.write(p.strip())
#    except: pass
#
#def interactive_select_port(default_port):
#    port=default_port; baud=BAUD; parity_name=PARITY_NAME
#    data_bits=DATA_BITS; stop_bits=STOP_BITS; flow_ctrl=FLOW_CTRL; enter_mode=ENTER_MODE
#    last=load_last_port()
#    if last: default_port=last
#    if not INTERACTIVE_SELECT:
#        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#    print(ui_head("serial interactive config"))
#    if list_ports:
#        ports=list(list_ports.comports())
#        if ports:
#            for idx,p in enumerate(ports,1):
#                print(f" {idx}. {p.device:<8} {p.description} ({p.hwid})")
#        else:
#            print(" (no detected ports)")
#    val=input(f"port [{default_port}]: ").strip()
#    if val: port=val
#    val=input(f"baud [{baud}]: ").strip()
#    if val.isdigit(): baud=int(val)
#    plist=["none","even","odd","mark","space"]
#    val=input(f"parity {plist} [{parity_name}]: ").strip().lower()
#    if val in plist: parity_name=val
#    val=input(f"data bits (7/8) [{data_bits}]: ").strip()
#    if val in ("7","8"): data_bits=int(val)
#    val=input(f"stop bits (1/2) [{STOP_BITS}]: ").strip()
#    if val in ("1","2"): stop_bits=int(val)
#    flist=["none","rtscts","dsrdtr","x"]
#    val=input(f"flowctrl {flist} [{flow_ctrl}]: ").strip().lower()
#    if val in flist: flow_ctrl=val
#    emlist=["CR","CRLF","LF","NONE"]
#    val=input(f"enter mode {emlist} [{enter_mode}]: ").strip().upper()
#    if val in emlist: enter_mode=val
#    save_last_port(port)
#    print(ui_line())
#    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#
## ================== Hotkey Thread ======================================
#class HotkeyThread(threading.Thread):
#    def __init__(self,
#                 play_callback,
#                 show_all_callback,
#                 combo_list_callback,
#                 run_single_combo_callback,
#                 stop_event):
#        super().__init__(daemon=True)
#        self.play_callback=play_callback
#        self.show_all_callback=show_all_callback
#        self.combo_list_callback=combo_list_callback
#        self.run_single_combo_callback=run_single_combo_callback
#        self.stop_event=stop_event
#        import ctypes
#        self.ctypes=ctypes
#        self.user32=ctypes.WinDLL("user32", use_last_error=True)
#        self.VK_CTRL=0x11; self.VK_S=0x53
#        self.VK_C=0x43; self.VK_B=0x42; self.VK_L=0x4C
#        self.VK_0_9=list(range(0x30,0x3A))
#        self.VK_NUM_0_9=list(range(0x60,0x6A))
#        self.VK_A_Z=list(range(0x41,0x5B))
#        self.prev_digit_down={vk:False for vk in self.VK_0_9+self.VK_NUM_0_9}
#        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
#        self.prev_s_down=False
#        self.prev_cl_combo_list=False
#    def key_down(self,vk):
#        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
#    def run(self):
#        while not self.stop_event.is_set():
#            ctrl=self.key_down(self.VK_CTRL)
#            s_now=ctrl and self.key_down(self.VK_S)
#            if s_now and not self.prev_s_down:
#                print(); self.show_all_callback()
#            self.prev_s_down=s_now
#            c_now=self.key_down(self.VK_C); b_now=self.key_down(self.VK_B)
#            cb_now=c_now and b_now
#            if cb_now:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        self.run_single_combo_callback(key)
#                    self.prev_digit_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#            l_now=self.key_down(self.VK_L); cl_now=c_now and l_now
#            if cl_now and not self.prev_cl_combo_list:
#                print(); self.combo_list_callback()
#            self.prev_cl_combo_list=cl_now
#            if ctrl:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        print(); self.play_callback(key.lower())
#                    self.prev_digit_down[vk]=now
#                for vk in self.VK_A_Z:
#                    now=self.key_down(vk)
#                    if now and not self.prev_letter_down[vk]:
#                        key=chr(vk).lower()
#                        print(); self.play_callback(key)
#                    self.prev_letter_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
#                self.prev_s_down=False
#            time.sleep(HOTKEY_POLL_INTERVAL_SEC)
#
## ================== Main ================================================
#def main():
#    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO
#    global SCRIPT_PROMPT_TIMEOUT_SEC, SCRIPT_WAIT_PROMPT, FAST_PLAY_MODE
#    global PROMPT_PATTERNS
#    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
#    user_cfg=load_user_config()
#
#    # restore
#    for k in ("char_delay_ms","line_delay_ms","script_char_delay_ms","prompt_timeout_sec"):
#        if k in user_cfg:
#            try:
#                v=float(user_cfg[k])
#                if v>=0:
#                    if k=="char_delay_ms": globals()['CHAR_DELAY_MS']=v
#                    elif k=="line_delay_ms": globals()['LINE_DELAY_MS']=v
#                    elif k=="script_char_delay_ms": SAFE_SCRIPT_CHAR_DELAY_MS=v
#                    elif k=="prompt_timeout_sec": SCRIPT_PROMPT_TIMEOUT_SEC=v
#            except: pass
#    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
#    if "script_local_echo" in user_cfg: SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
#    if "script_wait_prompt" in user_cfg: SCRIPT_WAIT_PROMPT=bool(user_cfg["script_wait_prompt"])
#    if "fast_play_mode" in user_cfg: FAST_PLAY_MODE=bool(user_cfg["fast_play_mode"])
#    if "prompt_patterns" in user_cfg:
#        pp=user_cfg["prompt_patterns"]
#        if isinstance(pp,list):
#            cleaned=[str(p) for p in pp if isinstance(p,str) and p.strip()]
#            if cleaned: PROMPT_PATTERNS=cleaned[:]
#
#    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
#    init_baud=cfg_ini.get("BaudRate",BAUD)
#    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
#    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
#    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
#    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
#    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
#    if init_enter not in ("CR","CRLF","LF","NONE"): init_enter="CR"
#
#    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)
#
#    parity_map={"even":serial.PARITY_EVEN,"odd":serial.PARITY_ODD,"none":serial.PARITY_NONE,
#                "mark":serial.PARITY_MARK,"space":serial.PARITY_SPACE}
#    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
#    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
#    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE
#
#    if fc in ("rtscts","hard"):
#        rtscts,dsrdtr,xonxoff=True,False,False
#    elif fc=="dsrdtr":
#        rtscts,dsrdtr,xonxoff=False,True,False
#    elif fc=="x":
#        rtscts,dsrdtr,xonxoff=False,False,True
#    else:
#        rtscts=dsrdtr=xonxoff=False
#
#    char_delay=float(globals()['CHAR_DELAY_MS']); line_delay=float(globals()['LINE_DELAY_MS'])
#
#    try:
#        ser=serial.Serial(port,baud,timeout=TIMEOUT,
#                          bytesize=bytesize,parity=parity,stopbits=stopbits,
#                          rtscts=rtscts,dsrdtr=dsrdtr,xonxoff=xonxoff,write_timeout=1)
#    except serial.SerialException as e:
#        print(f"[err] cannot open {port}: {e}"); return
#
#    try:
#        if ASSERT_DTR: ser.setDTR(True)
#        if ASSERT_RTS: ser.setRTS(True)
#    except Exception as e:
#        print(f"[warn] set dtr/rts failed: {e}")
#
#    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
#        try:
#            ser.reset_input_buffer(); ser.reset_output_buffer()
#        except Exception as e: print(f"[warn] clear buffers failed: {e}")
#
#    if not NO_BANNER:
#        patt_summary=" | ".join(PROMPT_PATTERNS)
#        lines=[
#            ui_kv("port",ser.port),
#            ui_kv("baud",ser.baudrate),
#            ui_kv("data/parity/stop",f"{data_bits}/{parity_name}/{stop_bits_val}"),
#            ui_kv("flow","rtscts="+str(rtscts)+" dsrdtr="+str(dsrdtr)+" xonxoff="+str(xonxoff)),
#            ui_kv("enter",enter_mode),
#            ui_kv("char_delay",f"{char_delay} ms"),
#            ui_kv("line_delay",f"{line_delay} ms"),
#            ui_kv("script_char_min",f"{SAFE_SCRIPT_CHAR_DELAY_MS} ms"),
#            ui_kv("hex_tx","on" if TX_HEX else "off"),
#            ui_kv("script_echo","on" if SCRIPT_LOCAL_ECHO else "off"),
#            ui_kv("prompt_wait","on" if SCRIPT_WAIT_PROMPT else "off"),
#            ui_kv("prompt_timeout",f"{SCRIPT_PROMPT_TIMEOUT_SEC}s"),
#            ui_kv("fastplay","on" if FAST_PLAY_MODE else "off"),
#            ui_kv("prompts",patt_summary)
#        ]
#        ui_print_block("session", lines)
#        print("[info] type /help for commands")
#
#    log_file=None
#    if LOG_PATH:
#        try:
#            log_file=open(LOG_PATH,"a",encoding="utf-8")
#            print(f"[info] logging -> {LOG_PATH}")
#        except Exception as e:
#            print(f"[warn] log open failed: {e}")
#
#    reader=SerialReaderThread(
#        ser,encoding=ENCODING,hex_dump=HEX_DUMP_RX,raw=RAW_RX,
#        log_file=log_file,quiet=QUIET_RX
#    )
#    reader.start()
#    send_lock=threading.Lock()
#
#    def persist_user():
#        user_cfg={
#            "char_delay_ms":char_delay,
#            "line_delay_ms":line_delay,
#            "tx_hex":TX_HEX,
#            "script_char_delay_ms":SAFE_SCRIPT_CHAR_DELAY_MS,
#            "script_local_echo":SCRIPT_LOCAL_ECHO,
#            "script_wait_prompt":SCRIPT_WAIT_PROMPT,
#            "prompt_timeout_sec":SCRIPT_PROMPT_TIMEOUT_SEC,
#            "fast_play_mode":FAST_PLAY_MODE,
#            "prompt_patterns":PROMPT_PATTERNS
#        }
#        save_user_config(user_cfg)
#
#    def line_suffix():
#        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]
#
#    def send_bytes(data:bytes, tag="tx", safe=False, local_echo_line=None):
#        if not data: return
#        per_char_delay=char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
#        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
#            print(local_echo_line)
#        if per_char_delay>0 and len(data)>1:
#            for i,b in enumerate(data):
#                with send_lock:
#                    try: ser.write(bytes([b])); ser.flush()
#                    except serial.SerialException as e:
#                        print(f"[err] tx failed: {e}"); return
#                if TX_HEX and not QUIET_RX: print(f"[{tag}] {format_hex(bytes([b]))}")
#                if log_file:
#                    ts=datetime.now().strftime("%H:%M:%S.%f")[:-3]
#                    try: log_file.write(f"[{ts}] {tag.upper()} {format_hex(bytes([b]))}\n"); log_file.flush()
#                    except: pass
#                if i<len(data)-1: time.sleep(per_char_delay/1000.0)
#        else:
#            with send_lock:
#                try: ser.write(data); ser.flush()
#                except serial.SerialException as e:
#                    print(f"[err] tx failed: {e}"); return
#            if TX_HEX and not QUIET_RX: print(f"[{tag}] {format_hex(data)}")
#            if log_file:
#                ts=datetime.now().strftime("%H:%M:%S.%f")[:-3]
#                try: log_file.write(f"[{ts}] {tag.upper()} {format_hex(data)}\n"); log_file.flush()
#                except: pass
#        if line_delay>0 and tag=="tx": time.sleep(line_delay/1000.0)
#
#    class ScriptContext:
#        def __init__(self):
#            self.last_prompt_seq=get_prompt_seq()
#            self.first_send=True
#        def wait_ready_if_needed(self):
#            if FAST_PLAY_MODE: return
#            if not SCRIPT_WAIT_PROMPT: return
#            if self.first_send:
#                self.first_send=False; return
#            prev=self.last_prompt_seq
#            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
#        def note_after_send(self): pass
#
#    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        try: body=text.encode(ENCODING,errors="replace")
#        except Exception as e: print(f"[warn] encode failed: {e}"); return
#        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    def send_enter_only(safe=False, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        send_bytes(line_suffix(), tag="tx-empty", safe=safe)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    # Data holders
#    slot_cmds={k:None for k in ALL_SLOTS}; load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
#    global_combos={}; load_global_combos(COMBO_SAVE_FILE, global_combos)
#    i2c_dump_slots={str(i):None for i in range(MAX_I2C_DUMPS)}; load_i2c_dumps(I2C_DUMP_SAVE_FILE, i2c_dump_slots)
#    cmp_history=load_cmp_history(DUMPCMP_HISTORY_FILE)
#    load_cmp_results(DUMPCMP_RESULTS_FILE)
#
#    # Display helpers
#    def show_slots():
#        rows=[]
#        for k in DIGIT_SLOTS+LETTER_SLOTS:
#            v=slot_cmds.get(k)
#            if v is None:
#                rows.append(f"{k}: (empty)")
#            else:
#                t=v.get("type")
#                if t=="enter":
#                    rows.append(f"{k}: <enter>")
#                elif t=="combo":
#                    rows.append(f"{k}: <combo {v.get('seq','')}>")
#                else:
#                    d=v.get("data","")
#                    one=d.splitlines()[0] if d else ""
#                    more="…" if "\n" in d else ""
#                    rows.append(f"{k}: {one[:50]}{more}")
#        ui_print_block("slots", rows)
#
#    def show_global_combos():
#        rows=[]
#        for d in DIGIT_SLOTS:
#            if d in global_combos:
#                rows.append(f"{d}: {global_combos[d]}")
#            else:
#                rows.append(f"{d}: (empty)")
#        ui_print_block("digit combos", rows)
#
#    def dumplist():
#        rows=[f"{d}: {(str(len(v))+' lines') if v else '(empty)'}" for d,v in i2c_dump_slots.items()]
#        ui_print_block("i2c dump slots", rows)
#
#    def dump_show(d):
#        v=i2c_dump_slots.get(d)
#        if not v:
#            print(f"[dumps] slot {d} empty"); return
#        print(ui_head(f"dump slot {d} ({len(v)} lines)"))
#        for ln in v:
#            print(ln)
#        print(ui_line())
#
#    HEADER_LINE = "     " + " ".join(f"{i:02x}" for i in range(16))
#    ROW_ADDRS   = [f"{i:02x}" for i in range(0,256,16)]
#
#    def _parse_dump_to_matrix(lines):
#        matrix={}
#        for ln in lines:
#            if ln.startswith("#NO_HEADER#"):
#                continue
#            m=re.match(r'^([0-9A-Fa-f]{2}):\s+(.*)$', ln)
#            if not m: continue
#            addr=m.group(1).lower()
#            rest=m.group(2).strip()
#            bytes_list=[b for b in rest.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', b)]
#            if len(bytes_list)<16: bytes_list+=["--"]*(16-len(bytes_list))
#            elif len(bytes_list)>16: bytes_list=bytes_list[:16]
#            matrix[addr]=[b.upper() for b in bytes_list]
#        for a in ROW_ADDRS:
#            if a not in matrix: matrix[a]=["--"]*16
#        return matrix
#
#    def _hex_to_bin(h):
#        try:
#            bits=f"{int(h,16):08b}"
#            return bits[:4]+"_"+bits[4:]
#        except:
#            return "----_----"
#
#    def _store_cmp_result(entry):
#        _dumpcmp_results.append(entry)
#        if len(_dumpcmp_results)>MAX_CMP_RESULTS_ENTRIES:
#            del _dumpcmp_results[:-MAX_CMP_RESULTS_ENTRIES]
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#
#    def _dump_compare_single(a,b,*,suppress_end=False):
#        da=i2c_dump_slots.get(a); db=i2c_dump_slots.get(b)
#        if not da: print(f"[dumpcmp] slot {a} empty"); return None
#        if not db: print(f"[dumpcmp] slot {b} empty"); return None
#        mA=_parse_dump_to_matrix(da); mB=_parse_dump_to_matrix(db)
#        changed_bytes=0; changed_rows=0
#        hex_lines=[]; bin_lines=[]
#        print("hex"); hex_lines.append("hex")
#        print(f" disk:{a}"); hex_lines.append(f" disk:{a}")
#        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]; row_tokens=[]; row_changed=False
#            for i in range(16):
#                if rowA[i]==rowB[i]:
#                    row_tokens.append("XX")
#                else:
#                    row_tokens.append(rowA[i]); changed_bytes+=1; row_changed=True
#            if row_changed: changed_rows+=1
#            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); hex_lines.append(ln)
#        print(f"disk:{b}"); hex_lines.append(f"disk:{b}")
#        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=["XX" if rowA[i]==rowB[i] else rowB[i] for i in range(16)]
#            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); hex_lines.append(ln)
#        print(); bin_lines.append("")
#        print("binary"); bin_lines.append("binary")
#        print(f"disk:{a}"); bin_lines.append(f"disk:{a}")
#        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowA[i]) for i in range(16)]
#            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); bin_lines.append(ln)
#        print(f"disk:{b}"); bin_lines.append(f"disk:{b}")
#        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowB[i]) for i in range(16)]
#            ln=f"{addr}:  {' '.join(row_tokens)}"; print(ln); bin_lines.append(ln)
#        if not suppress_end: print("[dumpcmp] end")
#        entry={
#            "timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
#            "a":a,"b":b,
#            "changed_rows":changed_rows,
#            "changed_bytes":changed_bytes,
#            "hex_lines":hex_lines,
#            "binary_lines":bin_lines
#        }
#        _store_cmp_result(entry)
#        return {"a":a,"b":b,"changed_rows":changed_rows,"changed_bytes":changed_bytes}
#
#    def dump_compare(a,b):
#        return _dump_compare_single(a,b)
#
#    def parse_multi_pairs(arg_str):
#        parts=[p.strip() for p in arg_str.split(",") if p.strip()]
#        pairs=[]
#        for p in parts:
#            toks=p.split()
#            if len(toks)!=2:
#                print(f"[dumpcmp] skip invalid pair '{p}'"); continue
#            da,db=toks
#            if da in DIGIT_SLOTS and db in DIGIT_SLOTS:
#                pairs.append((da,db))
#            else:
#                print(f"[dumpcmp] skip non-digit pair '{p}'")
#        return pairs
#
#    def multi_dump_compare(pairs):
#        if not pairs:
#            print("[dumpcmp] no valid pairs"); return
#        session_stats=[]
#        print(f"[dumpcmp] multi {len(pairs)} pair(s): {', '.join(f'{a}-{b}' for a,b in pairs)}")
#        for idx,(a,b) in enumerate(pairs,1):
#            print(f"\n[dumpcmp] pair {idx}/{len(pairs)} {a} vs {b}")
#            stats=_dump_compare_single(a,b,suppress_end=True)
#            if stats:
#                print("[dumpcmp] end"); session_stats.append(stats)
#        entry={
#            "timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
#            "pairs":[{"a":s["a"],"b":s["b"],"changed_rows":s["changed_rows"],"changed_bytes":s["changed_bytes"]} for s in session_stats]
#        }
#        cmp_history.append(entry); save_cmp_history(DUMPCMP_HISTORY_FILE,cmp_history)
#        total_changed=sum(s["changed_bytes"] for s in session_stats)
#        print(f"\n[dumpcmp] summary: {len(session_stats)} compared  total_changed={total_changed}")
#        for s in session_stats:
#            print(f"  {s['a']}-{s['b']}: rows={s['changed_rows']} bytes={s['changed_bytes']}")
#
#    def show_cmp_history(limit=10):
#        tail=cmp_history[-limit:]
#        print(ui_head("multi compare history"))
#        if not tail: print(" (none)")
#        else:
#            for idx,entry in enumerate(tail,1):
#                ts=entry.get("timestamp","?")
#                pairs_txt=", ".join(f"{p['a']}-{p['b']}:{p['changed_bytes']}" for p in entry.get("pairs",[]))
#                print(f"{idx:2}. {ts}  {pairs_txt}")
#        print(ui_line())
#
#    def show_cmp_results(limit=10):
#        tail=_dumpcmp_results[-limit:]
#        print(ui_head("stored pair results"))
#        if not tail: print(" (none)")
#        else:
#            for i,entry in enumerate(tail,1):
#                print(f"{i:2}. {entry.get('timestamp','?')} {entry.get('a')}-{entry.get('b')} "
#                      f"bytes={entry.get('changed_bytes')} rows={entry.get('changed_rows')}")
#        print(ui_line())
#
#    def clear_cmp_results():
#        global _dumpcmp_results
#        _dumpcmp_results=[]
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#        print("[cmpres] cleared")
#
#    def overview():
#        show_slots(); show_global_combos(); dumplist()
#
#    def print_help():
#        print(ui_head("help"))
#        print("""groups:
#  slots          : /setx /combox /enterx /clrx ox /slots /slotsave /slotload
#  combos (digit) : /cset d <seq> /clist /crun d /cclear d /crun_all /csave /cload
#  i2cdump        : /dumpsave d /dumpshow d /dumplist
#                   /dumpcmp a b
#                   /dumpcmp a b,c d,e f ... OR /dumpcmpmulti ...
#  compare meta   : /cmphist [n] /cmpres [n] /cmpresclear
#  timing         : /delay /scriptdelay /linedelay
#  script flow    : /fastplay on|off /scriptwait on|off /promptime [sec]
#  mode toggles   : /hex on|off /scriptecho on|off
#  general        : /help /quit
#
#notes:
#  - all commands lowercase
#  - fastplay skips ALL prompt waits
#  - prompt patterns editable in .console_config.json (key: prompt_patterns)
#""")
#        print(ui_line())
#
#    # slot execution
#    def play_slot_recursive(idx_char, depth, visited, script_ctx):
#        if depth>40:
#            print("[play] depth limit"); return
#        if idx_char not in slot_cmds:
#            print(f"[play] slot {idx_char} not found"); return
#        v=slot_cmds[idx_char]
#        if v is None:
#            print(f"[play] slot {idx_char} empty"); return
#        if id(v) in visited:
#            print(f"[play] cycle at {idx_char}"); return
#        visited.add(id(v))
#        t=v.get("type")
#        if t=="enter":
#            send_enter_only(safe=True, script_ctx=script_ctx)
#        elif t=="combo":
#            for c in v.get("seq",""):
#                if c in slot_cmds:
#                    play_slot_recursive(c, depth+1, visited, script_ctx)
#        else:
#            data=v.get("data","")
#            parts=data.split(TOKEN_ENTER)
#            for pi,segment in enumerate(parts):
#                lines=segment.splitlines()
#                if not lines and segment=="":
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#                for line in lines:
#                    if line.strip()=="" and line!="":
#                        send_enter_only(safe=True, script_ctx=script_ctx)
#                    elif line!="":
#                        send_line(line,safe=True,
#                                  local_echo=f"[run] {line}" if SCRIPT_LOCAL_ECHO else None,
#                                  script_ctx=script_ctx)
#                if pi<len(parts)-1:
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#        visited.remove(id(v))
#
#    def play_slot(k):
#        if k not in slot_cmds:
#            print(f"[play] invalid slot {k}"); return
#        print(f"[play] slot {k}")
#        ctx=ScriptContext()
#        play_slot_recursive(k,0,set(),ctx)
#
#    def run_global_combo(d):
#        if d not in global_combos:
#            print(f"[combo] digit {d} undefined"); return
#        seq=global_combos[d]; print(f"[combo] run {d}: {seq}")
#        ctx=ScriptContext()
#        for c in seq:
#            if c in slot_cmds:
#                play_slot_recursive(c,0,set(),ctx)
#
#    def run_all_global_combos():
#        defined=[d for d in DIGIT_SLOTS if d in global_combos]
#        if not defined:
#            print("[combo] none defined"); return
#        print("[combo] run all:")
#        ctx=ScriptContext()
#        for d in defined:
#            seq=global_combos[d]; print(f"  {d}: {seq}")
#            for c in seq:
#                if c in slot_cmds:
#                    play_slot_recursive(c,0,set(),ctx)
#
#    def run_single_combo_via_hotkey(d):
#        if d in global_combos:
#            print(f"[combo] (hotkey) {d}")
#            run_global_combo(d)
#        else:
#            print(f"[combo] (hotkey) {d} undefined")
#
#    # hotkeys
#    stop_hotkey=threading.Event()
#    hotkey_thread=None
#    if os.name=='nt':
#        try:
#            hotkey_thread=HotkeyThread(
#                play_callback=play_slot,
#                show_all_callback=overview,
#                combo_list_callback=show_global_combos,
#                run_single_combo_callback=run_single_combo_via_hotkey,
#                stop_event=stop_hotkey
#            )
#            hotkey_thread.start()
#        except Exception as e:
#            print(f"[warn] hotkey thread failed: {e}")
#
#    # ================== Command loop ==================================
#    try:
#        while True:
#            try:
#                line=input()
#            except EOFError:
#                break
#            stripped=line.strip()
#            lower=stripped.lower()
#
#            if lower=="/help":
#                print_help(); continue
#
#            # i2c dump commands
#            if lower.startswith("/dumpsave"):
#                parts=lower.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[dumps] usage: /dumpsave <digit>")
#                else:
#                    d=parts[1]
#                    if _last_captured_dump:
#                        i2c_dump_slots[d]=_last_captured_dump[:]
#                        print(f"[dumps] saved to slot {d} ({len(_last_captured_dump)} lines)")
#                        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#                    else:
#                        print("[dumps] no captured dump")
#                continue
#
#            if lower.startswith("/dumpshow"):
#                parts=lower.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[dumps] usage: /dumpshow <digit>")
#                else:
#                    dump_show(parts[1])
#                continue
#
#            if lower=="/dumplist":
#                dumplist(); continue
#
#            # dump compare
#            if lower.startswith("/dumpcmp") or lower.startswith("/dumpcmpmulti"):
#                cmd,*rest=lower.split(None,1)
#                if not rest:
#                    print("[dumpcmp] usage: /dumpcmp a b | /dumpcmp a b,c d,e f")
#                    continue
#                rem=rest[0].strip()
#                if ',' in rem or len(rem.split())>2:
#                    if ',' not in rem:
#                        toks=rem.split()
#                        if len(toks)>=4 and len(toks)%2==0:
#                            rem=",".join(f"{toks[i]} {toks[i+1]}" for i in range(0,len(toks),2))
#                    pairs=parse_multi_pairs(rem)
#                    multi_dump_compare(pairs)
#                else:
#                    p=rem.split()
#                    if len(p)!=2 or p[0] not in DIGIT_SLOTS or p[1] not in DIGIT_SLOTS:
#                        print("[dumpcmp] usage: /dumpcmp <a> <b>")
#                    else:
#                        dump_compare(p[0],p[1])
#                continue
#
#            if lower.startswith("/cmphist"):
#                parts=lower.split()
#                lim=10
#                if len(parts)==2 and parts[1].isdigit():
#                    lim=max(1,min(100,int(parts[1])))
#                show_cmp_history(lim)
#                continue
#
#            if lower.startswith("/cmpresclear"):
#                confirm=input("confirm clear compare results (yes): ").strip().lower()
#                if confirm=="yes":
#                    clear_cmp_results()
#                else:
#                    print("[cmpres] cancelled")
#                continue
#
#            if lower.startswith("/cmpres"):
#                parts=lower.split()
#                lim=10
#                if len(parts)==2 and parts[1].isdigit():
#                    lim=max(1,min(200,int(parts[1])))
#                show_cmp_results(lim)
#                continue
#
#            # timing / modes
#            if lower.startswith("/delay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[delay] {char_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        char_delay=v; print(f"[delay] -> {char_delay} ms"); persist_user()
#                    except: print("[delay] invalid")
#                continue
#
#            if lower.startswith("/scriptdelay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptdelay] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[scriptdelay] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
#                    except: print("[scriptdelay] invalid")
#                continue
#
#            if lower.startswith("/linedelay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[linedelay] {line_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        line_delay=v; print(f"[linedelay] -> {line_delay} ms"); persist_user()
#                    except: print("[linedelay] invalid")
#                continue
#
#            if lower.startswith("/scriptecho"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptecho] {'on' if SCRIPT_LOCAL_ECHO else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        SCRIPT_LOCAL_ECHO=(arg=="on")
#                        print(f"[scriptecho] -> {'on' if SCRIPT_LOCAL_ECHO else 'off'}"); persist_user()
#                    else:
#                        print("[scriptecho] use /scriptecho on|off")
#                continue
#
#            if lower.startswith("/hex"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[hex] {'on' if TX_HEX else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        TX_HEX=(arg=="on")
#                        print(f"[hex] -> {'on' if TX_HEX else 'off'}"); persist_user()
#                    else:
#                        print("[hex] use /hex on|off")
#                continue
#
#            if lower.startswith("/fastplay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[fastplay] {'on' if FAST_PLAY_MODE else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        FAST_PLAY_MODE=(arg=="on")
#                        print(f"[fastplay] -> {'on' if FAST_PLAY_MODE else 'off'}"); persist_user()
#                    else:
#                        print("[fastplay] use /fastplay on|off")
#                continue
#
#            if lower.startswith("/scriptwait"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptwait] {'on' if SCRIPT_WAIT_PROMPT else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        SCRIPT_WAIT_PROMPT=(arg=="on")
#                        print(f"[scriptwait] -> {'on' if SCRIPT_WAIT_PROMPT else 'off'}"); persist_user()
#                    else:
#                        print("[scriptwait] use /scriptwait on|off")
#                continue
#
#            if lower.startswith("/promptime"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[promptime] {SCRIPT_PROMPT_TIMEOUT_SEC} s")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        SCRIPT_PROMPT_TIMEOUT_SEC=v; print(f"[promptime] -> {SCRIPT_PROMPT_TIMEOUT_SEC} s"); persist_user()
#                    except: print("[promptime] invalid")
#                continue
#
#            # slot persistence
#            if lower=="/slotsave":
#                save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
#            if lower=="/slotload":
#                load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue
#
#            # combos
#            if lower=="/clist":
#                show_global_combos(); continue
#            if lower.startswith("/cset "):
#                parts=stripped.split(None,2)
#                if len(parts)<3:
#                    print("[combo] usage: /cset <digit> <seq>")
#                else:
#                    digit=parts[1]
#                    if not (digit.isdigit() and len(digit)==1):
#                        print("[combo] digit must be 0-9")
#                    else:
#                        seq="".join(ch for ch in parts[2] if ch.isalnum())
#                        global_combos[digit]=seq
#                        print(f"[combo] {digit} = {seq}")
#                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                continue
#            if lower.startswith("/crun "):
#                digit=lower.split(None,1)[1]
#                run_global_combo(digit); continue
#            if lower.startswith("/cclear "):
#                digit=lower.split(None,1)[1]
#                if digit in global_combos:
#                    del global_combos[digit]; print(f"[combo] cleared {digit}")
#                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                else:
#                    print("[combo] not defined")
#                continue
#            if lower=="/csave":
#                save_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if lower=="/cload":
#                load_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if lower=="/crun_all":
#                run_all_global_combos(); continue
#
#            # general
#            if lower=="/quit":
#                print("[info] quitting")
#                break
#            if lower=="/slots":
#                show_slots(); continue
#
#            # slot definitions
#            if lower.startswith("/enter") and len(lower)==7:
#                key=lower[6]
#                if key in slot_cmds:
#                    slot_cmds[key]={"type":"enter"}
#                    print(f"[set] slot {key} = <enter>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/combo") and len(lower)>=7:
#                key=lower[6]
#                if key in slot_cmds:
#                    parts=stripped.split(None,1)
#                    seq=""
#                    if len(parts)>1:
#                        seq="".join(ch for ch in parts[1] if ch.isalnum())
#                    slot_cmds[key]={"type":"combo","seq":seq}
#                    print(f"[set] slot {key} = <combo {seq}>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/set") and len(lower)>=5:
#                key=lower[4]
#                if key in slot_cmds:
#                    parts=stripped.split(None,1)
#                    data=parts[1] if len(parts)>1 else ""
#                    slot_cmds[key]={"type":"raw","data":data}
#                    print(f"[set] slot {key} len={len(data)}")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/clr") and len(lower)==5:
#                key=lower[4]
#                if key in slot_cmds:
#                    slot_cmds[key]=None
#                    print(f"[clr] slot {key} cleared")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            # run slot alias: oX
#            if len(lower)==2 and lower[0]=='o':
#                key=lower[1]
#                if key in slot_cmds:
#                    play_slot(key)
#                continue
#
#            # blank line -> ENTER
#            if line=="":
#                send_enter_only(safe=False); continue
#
#            # normal user input
#            try:
#                body=line.encode(ENCODING,errors="replace")
#            except Exception as e:
#                print(f"[warn] encode failed: {e}")
#                continue
#            send_bytes(body+line_suffix(), safe=False, tag="tx")
#
#    except KeyboardInterrupt:
#        print("\n[info] keyboardinterrupt")
#    finally:
#        persist_user()
#        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#        save_cmp_history(DUMPCMP_HISTORY_FILE, cmp_history)
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#        if 'hotkey_thread' in locals() and hotkey_thread:
#            stop_hotkey.set(); hotkey_thread.join(timeout=0.5)
#        reader.stop()
#        time.sleep(0.05)
#        try: ser.close()
#        except: pass
#        if 'log_file' in locals() and log_file:
#            try: log_file.close()
#            except: pass
#        print("[info] exit")
#
#if __name__ == "__main__":
#    main()
#    """






























    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
250904_0001_i2cdump_data_compare_pass
250904_0002_i2cdump_data_compare_binary_pass
250904_0003_i2cdump_data_multiple_compare_pass
250904_0004_i2cdump_data_multiple_compare_save_pass
250905_0001_prompt_timeout_save_pass

    """















    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
250904_0001_i2cdump_data_compare_pass
250904_0002_i2cdump_data_compare_binary_pass
250904_0003_i2cdump_data_multiple_compare_pass
250904_0004_i2cdump_data_multiple_compare_save_pass
250905_0001_prompt_timeout_save_pass

    """


#    """
##!/usr/bin/env python
## -*- coding: utf-8 -*-
#"""
#Serial console with:
# - Slots 0-9 + a-z (36 total)
# - Digit global combos (0-9)
# - Hotkeys: Ctrl+0..9 / Ctrl+a..z (play slot), Ctrl+S (show all), C+B+<digit> (single combo), C+L (list combos)
# - i2cdump capture & storage (/dumpsave /dumpshow /dumplist /dumpcmp)
# - Tolerant i2cdump capture (header or first data row, prompt line, overflow guard)
# - /dumpcmp:
#     hex
#       disk:<a> (unchanged => XX, changed => HEX from A)
#       disk:<b> (unchanged => XX, changed => HEX from B)
#     binary
#       disk:<a> row lines (unchanged => XX, changed => 8-bit binary xxxx_xxxx)
#       disk:<b> row lines (same rule)
#     (Binary section prints rows once per dump with changed bytes shown inline.)
# - Multi-compare support:
#     /dumpcmp 1 2,2 3,3 4  (sequential pairs)
#     /dumpcmpmulti alias
#     History summary stored in .dumpcmp_history.json
# - Detailed pair results stored in .dumpcmp_results.json (every pair, single or multi)
#   Entry fields: timestamp, a, b, changed_rows, changed_bytes, hex_lines[], binary_lines[]
# - Commands (ALL LOWERCASE):
#     /cmphist [n]      show multi-compare history
#     /cmpres  [n]      list recent stored pair results (metadata)
#     /cmpresclear      clear all stored detailed pair compare results (confirmation)
# - NEW RUNTIME TOGGLES (to avoid 5s wait between scripted slot/ combo steps):
#     /fastplay on|off          when ON: no prompt-wait between scripted slot steps
#     /scriptwait on|off        master enable/disable waiting for prompt pattern
#     /promptime [seconds]      show or set prompt wait timeout (default 5.0)
# - Receiver thread style preserved (only feed hook used, not altered)
#All command parsing expects lowercase now.
#"""
#
#import sys
#import serial
#import threading
#import time
#import os
#import json
#import re
#from datetime import datetime
#
#try:
#    from serial.tools import list_ports
#except ImportError:
#    list_ports = None
#
## ================== Config (overridden by saved user config) ==================
#PORT                    = "COM5"
#BAUD                    = 115200
#PARITY_NAME             = "none"
#DATA_BITS               = 8
#STOP_BITS               = 1
#FLOW_CTRL               = "none"
#ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
#ENCODING                = "utf-8"
#TIMEOUT                 = 0.05
#CHAR_DELAY_MS           = 0
#LINE_DELAY_MS           = 0
#ASSERT_DTR              = False
#ASSERT_RTS              = False
#CLEAR_BUFF_ON_OPEN      = False
#
#TX_HEX                  = True
#HEX_DUMP_RX             = False
#RAW_RX                  = False
#QUIET_RX                = False
#
#LOG_PATH                = None
#INI_PATH                = None
#NO_BANNER               = False
#
#INTERACTIVE_SELECT      = True
#REMEMBER_LAST           = True
#LAST_FILE_NAME          = ".last_port"
#
#SLOTS_SAVE_FILE         = ".slot_cmds.json"
#AUTO_SAVE_SLOTS         = True
#SHOW_SAVE_MESSAGE       = True
#
#COMBO_SAVE_FILE         = ".combo_defs.json"
#AUTO_SAVE_COMBOS        = True
#SHOW_COMBO_SAVE_MSG     = True
#
#USER_CONFIG_FILE        = ".console_config.json"
#AUTO_SAVE_CONFIG        = True
#
#I2C_DUMP_SAVE_FILE      = ".i2c_dumps.json"
#AUTO_SAVE_I2C_DUMPS     = True
#MAX_I2C_DUMPS           = 10   # 0-9
#
#SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
#SCRIPT_LOCAL_ECHO         = False
#
#PROMPT_PATTERN            = "i2c>"
#SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
#SCRIPT_WAIT_PROMPT        = True
#
#POST_PROMPT_STABILIZE_MS  = 5
#
#HOTKEY_POLL_INTERVAL_SEC  = 0.05
#TOKEN_ENTER               = "<ENTER>"
#
#DIGIT_SLOTS  = [str(i) for i in range(10)]
#LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
#ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS
#
#DUMPCMP_HISTORY_FILE     = ".dumpcmp_history.json"
#MAX_CMP_HISTORY_ENTRIES  = 200
#
#DUMPCMP_RESULTS_FILE     = ".dumpcmp_results.json"
#MAX_CMP_RESULTS_ENTRIES  = 400
#_dumpcmp_results         = []
#
## NEW: fast play mode (skip prompt waiting between scripted steps)
#FAST_PLAY_MODE           = False
#
## ======================================================================
## Utility
## ======================================================================
#def format_hex(data: bytes) -> str:
#    return " ".join(f"{b:02X}" for b in data)
#
#def parse_ini(path: str):
#    out = {}
#    if not path or not os.path.isfile(path):
#        return out
#    try:
#        with open(path, "r", encoding="utf-8", errors="ignore") as f:
#            for line in f:
#                line=line.strip()
#                if not line or line.startswith(";") or "=" not in line:
#                    continue
#                k,v=line.split("=",1)
#                k=k.strip(); v=v.strip()
#                kl=k.lower()
#                if kl in ("comport","baudrate","delayperchar","delayperline"):
#                    try: out[k]=int(v)
#                    except: pass
#                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
#                    out[k]=v
#    except Exception as e:
#        print(f"[WARN] INI parse failed: {e}")
#    return out
#
#def load_user_config():
#    if not os.path.isfile(USER_CONFIG_FILE):
#        return {}
#    try:
#        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        return data if isinstance(data,dict) else {}
#    except Exception as e:
#        print(f"[CFG] Load failed: {e}")
#        return {}
#
#def save_user_config(cfg):
#    if not AUTO_SAVE_CONFIG: return
#    try:
#        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
#            json.dump(cfg,f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[CFG] Save failed: {e}")
#
#def normalize_slot_value(v):
#    if v is None: return None
#    if isinstance(v,dict):
#        t=v.get("type")
#        if t=="raw":
#            d=v.get("data"); return {"type":"raw","data": d if isinstance(d,str) else ""}
#        if t=="enter": return {"type":"enter"}
#        if t=="combo":
#            seq=v.get("seq","")
#            if not isinstance(seq,str): seq=""
#            return {"type":"combo","seq":seq}
#        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
#    if isinstance(v,str): return {"type":"raw","data":v}
#    return {"type":"raw","data":str(v)}
#
#def load_slots_from_file(path, slot_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        changed=False
#        for k in slot_dict.keys():
#            if k in data:
#                slot_dict[k]=normalize_slot_value(data[k]); changed=True
#        if changed: print(f"[SLOTS] Loaded {path}")
#    except Exception as e:
#        print(f"[SLOTS] Load failed: {e}")
#
#def save_slots_to_file(path, slot_dict):
#    try:
#        out={k:(None if v is None else v) for k,v in slot_dict.items()}
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(out,f,ensure_ascii=False,indent=2)
#        if SHOW_SAVE_MESSAGE: print(f"[SLOTS] Saved -> {path}")
#    except Exception as e:
#        print(f"[SLOTS] Save failed: {e}")
#
#def load_global_combos(path, combo_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
#                    combo_dict[k]="".join(ch for ch in v if ch.isalnum())
#            print(f"[COMBO] Loaded {path} ({len(combo_dict)} items)")
#    except Exception as e:
#        print(f"[COMBO] Load failed: {e}")
#
#def save_global_combos(path, combo_dict):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
#        if SHOW_COMBO_SAVE_MSG: print(f"[COMBO] Saved -> {path}")
#    except Exception as e:
#        print(f"[COMBO] Save failed: {e}")
#
#def load_i2c_dumps(path, dump_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if k in dump_dict and isinstance(v,list):
#                    dump_dict[k]=v
#        print(f"[DUMPS] Loaded {path}")
#    except Exception as e:
#        print(f"[DUMPS] Load failed: {e}")
#
#def save_i2c_dumps(path, dump_dict):
#    if not AUTO_SAVE_I2C_DUMPS: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(dump_dict,f,ensure_ascii=False,indent=2)
#        print(f"[DUMPS] Saved -> {path}")
#    except Exception as e:
#        print(f"[DUMPS] Save failed: {e}")
#
#def load_cmp_history(path):
#    if not os.path.isfile(path): return []
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,list): return data
#    except Exception as e:
#        print(f"[CMPHIST] Load failed: {e}")
#    return []
#
#def save_cmp_history(path, hist):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(hist[-MAX_CMP_HISTORY_ENTRIES:],f,ensure_ascii=False,indent=2)
#        print(f"[CMPHIST] Saved -> {path}")
#    except Exception as e:
#        print(f"[CMPHIST] Save failed: {e}")
#
#def load_cmp_results(path):
#    global _dumpcmp_results
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,list):
#            _dumpcmp_results=data
#            print(f"[DUMPCMP] Loaded {len(_dumpcmp_results)} stored compare results")
#    except Exception as e:
#        print(f"[DUMPCMP] Results load failed: {e}")
#
#def save_cmp_results(path):
#    if not _dumpcmp_results: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(_dumpcmp_results[-MAX_CMP_RESULTS_ENTRIES:],f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[DUMPCMP] Results save failed: {e}")
#
## ======================================================================
## Prompt tracking
## ======================================================================
#prompt_lock=threading.Lock()
#prompt_seq=0
#def inc_prompt_if_in(text:str):
#    global prompt_seq
#    if PROMPT_PATTERN and PROMPT_PATTERN in text:
#        with prompt_lock:
#            prompt_seq+=1
#def get_prompt_seq():
#    with prompt_lock:
#        return prompt_seq
#def wait_for_next_prompt(prev_seq, timeout):
#    if not SCRIPT_WAIT_PROMPT: return prev_seq
#    deadline=time.time()+timeout
#    while time.time()<deadline:
#        cur=get_prompt_seq()
#        if cur>prev_seq:
#            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
#            return cur
#        time.sleep(0.01)
#    return get_prompt_seq()
#
## ======================================================================
## i2cdump capture logic
## ======================================================================
#_i2c_capture_buffer_fragment=""
#_i2c_capture_active=False
#_i2c_capture_lines=[]
#_last_captured_dump=None
#
#_I2C_HEADER_RE = re.compile(r'^\s+00(?:\s+[0-9A-Fa-f]{2}){15}\s*$')
#_I2C_DATA_ROW_RE = re.compile(r'^[0-9A-Fa-f]{2}:\s+([0-9A-Fa-f]{2}\s+){0,15}[0-9A-Fa-f]{2}\s*$')
#_LAST_ADDR = "f0"
#
#def _maybe_finalize_partial(reason:str):
#    global _i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if _i2c_capture_active and _i2c_capture_lines:
#        _last_captured_dump=_i2c_capture_lines[:]
#        print(f"\n[DUMPS] Captured ({reason}) {len(_last_captured_dump)} lines")
#    _i2c_capture_active=False
#    _i2c_capture_lines=[]
#
#def _i2c_capture_feed(chunk:str):
#    global _i2c_capture_buffer_fragment,_i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if not chunk: return
#    _i2c_capture_buffer_fragment += chunk
#    while True:
#        if '\n' not in _i2c_capture_buffer_fragment:
#            break
#        line,rest=_i2c_capture_buffer_fragment.split('\n',1)
#        _i2c_capture_buffer_fragment=rest
#        line=line.rstrip('\r')
#        if PROMPT_PATTERN and line.startswith(PROMPT_PATTERN):
#            if _i2c_capture_active:
#                _maybe_finalize_partial("prompt")
#            continue
#        if not _i2c_capture_active:
#            if _I2C_HEADER_RE.match(line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=[line]
#                continue
#            if re.match(r'^00:\s', line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=["#NO_HEADER#"]
#            else:
#                continue
#        if _i2c_capture_active:
#            if _I2C_DATA_ROW_RE.match(line) or line==_i2c_capture_lines[0]:
#                if line != _i2c_capture_lines[0]:
#                    _i2c_capture_lines.append(line)
#            else:
#                if line.strip():
#                    _i2c_capture_lines.append(line)
#            if line.lower().startswith(_LAST_ADDR + ":"):
#                _last_captured_dump=_i2c_capture_lines[:]
#                print(f"\n[DUMPS] Captured i2cdump ({len(_last_captured_dump)} lines)")
#                _i2c_capture_active=False
#                _i2c_capture_lines=[]
#                continue
#            if len(_i2c_capture_lines) > 60:
#                _maybe_finalize_partial("overflow")
#                continue
#
## ======================================================================
## Receiver thread (unchanged logic)
## ======================================================================
#class SerialReaderThread(threading.Thread):
#    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
#        super().__init__(daemon=True)
#        self.ser=ser; self.encoding=encoding
#        self.hex_dump=hex_dump; self.raw=raw
#        self.log_file=log_file; self.quiet=quiet
#        self._running=True
#    def stop(self): self._running=False
#    def run(self):
#        while self._running and self.ser.is_open:
#            try:
#                data=self.ser.read(self.ser.in_waiting or 1)
#            except serial.SerialException as e:
#                print(f"[ERR] Serial exception: {e}")
#                break
#            if not data: continue
#            if self.log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try:
#                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n"); self.log_file.flush()
#                except Exception: pass
#            if self.quiet: continue
#            if self.hex_dump:
#                txt=format_hex(data)
#                print(f"[RX HEX] {txt}")
#                inc_prompt_if_in(txt)
#                _i2c_capture_feed(txt+"\n")
#            elif self.raw:
#                sys.stdout.buffer.write(data); sys.stdout.flush()
#                try:
#                    decoded=data.decode(self.encoding,errors="ignore")
#                    inc_prompt_if_in(decoded)
#                    _i2c_capture_feed(decoded)
#                except: pass
#            else:
#                try:
#                    text=data.decode(self.encoding,errors="replace")
#                except Exception:
#                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
#                print(text,end="",flush=True)
#                inc_prompt_if_in(text)
#                _i2c_capture_feed(text)
#
## ======================================================================
## Port selection
## ======================================================================
#def load_last_port():
#    if not REMEMBER_LAST: return None
#    try:
#        if os.path.isfile(LAST_FILE_NAME):
#            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
#                v=f.read().strip()
#                if v: return v
#    except: pass
#    return None
#def save_last_port(p):
#    if not REMEMBER_LAST: return
#    try:
#        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
#            f.write(p.strip())
#    except: pass
#
#def interactive_select_port(default_port):
#    port=default_port; baud=BAUD; parity_name=PARITY_NAME
#    data_bits=DATA_BITS; stop_bits=STOP_BITS; flow_ctrl=FLOW_CTRL; enter_mode=ENTER_MODE
#    last=load_last_port()
#    if last: default_port=last
#    if not INTERACTIVE_SELECT:
#        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#    print("=== Serial Interactive Config (Enter to keep default) ===")
#    if list_ports:
#        ports=list(list_ports.comports())
#        if ports:
#            print("Available ports:")
#            for idx,p in enumerate(ports,1):
#                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
#        else:
#            print("No COM ports detected.")
#    val=input(f"Port [{default_port}]: ").strip()
#    if val: port=val
#    val=input(f"Baud [{baud}]: ").strip()
#    if val.isdigit(): baud=int(val)
#    plist=["none","even","odd","mark","space"]
#    val=input(f"Parity {plist} [{parity_name}]: ").strip().lower()
#    if val in plist: parity_name=val
#    val=input(f"Data bits (7/8) [{data_bits}]: ").strip()
#    if val in ("7","8"): data_bits=int(val)
#    val=input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
#    if val in ("1","2"): stop_bits=int(val)
#    flist=["none","rtscts","dsrdtr","x"]
#    val=input(f"flowctrl {flist} [{flow_ctrl}]: ").strip().lower()
#    if val in flist: flow_ctrl=val
#    emlist=["CR","CRLF","LF","NONE"]
#    val=input(f"enter mode {emlist} [{enter_mode}]: ").strip().upper()
#    if val in emlist: enter_mode=val
#    save_last_port(port)
#    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#
## ======================================================================
## Hotkey Thread
## ======================================================================
#class HotkeyThread(threading.Thread):
#    def __init__(self,
#                 play_callback,
#                 show_all_callback,
#                 combo_list_callback,
#                 run_single_combo_callback,
#                 stop_event):
#        super().__init__(daemon=True)
#        self.play_callback=play_callback
#        self.show_all_callback=show_all_callback
#        self.combo_list_callback=combo_list_callback
#        self.run_single_combo_callback=run_single_combo_callback
#        self.stop_event=stop_event
#        import ctypes
#        self.ctypes=ctypes
#        self.user32=ctypes.WinDLL("user32", use_last_error=True)
#        self.VK_CTRL=0x11; self.VK_S=0x53
#        self.VK_C=0x43; self.VK_B=0x42; self.VK_L=0x4C
#        self.VK_0_9=list(range(0x30,0x3A))
#        self.VK_NUM_0_9=list(range(0x60,0x6A))
#        self.VK_A_Z=list(range(0x41,0x5B))
#        self.prev_digit_down={vk:False for vk in self.VK_0_9+self.VK_NUM_0_9}
#        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
#        self.prev_s_down=False
#        self.prev_cb=False
#        self.prev_cl_combo_list=False
#    def key_down(self,vk):
#        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
#    def run(self):
#        while not self.stop_event.is_set():
#            ctrl=self.key_down(self.VK_CTRL)
#            s_now=ctrl and self.key_down(self.VK_S)
#            if s_now and not self.prev_s_down:
#                print(); self.show_all_callback()
#            self.prev_s_down=s_now
#            c_now=self.key_down(self.VK_C); b_now=self.key_down(self.VK_B)
#            cb_now=c_now and b_now
#            if cb_now:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        self.run_single_combo_callback(key)
#                    self.prev_digit_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#            l_now=self.key_down(self.VK_L); cl_now=c_now and l_now
#            if cl_now and not self.prev_cl_combo_list:
#                print(); self.combo_list_callback()
#            self.prev_cl_combo_list=cl_now
#            if ctrl:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        print(); self.play_callback(key.lower())
#                    self.prev_digit_down[vk]=now
#                for vk in self.VK_A_Z:
#                    now=self.key_down(vk)
#                    if now and not self.prev_letter_down[vk]:
#                        key=chr(vk).lower()
#                        print(); self.play_callback(key)
#                    self.prev_letter_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
#                self.prev_s_down=False
#            time.sleep(HOTKEY_POLL_INTERVAL_SEC)
#
## ======================================================================
## Main
## ======================================================================
#def main():
#    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO, _last_captured_dump
#    global SCRIPT_PROMPT_TIMEOUT_SEC, SCRIPT_WAIT_PROMPT, FAST_PLAY_MODE
#    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
#    user_cfg=load_user_config()
#
#    # restore user settings if present
#    if "char_delay_ms" in user_cfg:
#        try: globals()['CHAR_DELAY_MS']=float(user_cfg["char_delay_ms"])
#        except: pass
#    if "line_delay_ms" in user_cfg:
#        try: globals()['LINE_DELAY_MS']=float(user_cfg["line_delay_ms"])
#        except: pass
#    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
#    if "script_char_delay_ms" in user_cfg:
#        try:
#            v=float(user_cfg["script_char_delay_ms"])
#            if v>=0: SAFE_SCRIPT_CHAR_DELAY_MS=v
#        except: pass
#    if "script_local_echo" in user_cfg:
#        SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
#    if "script_wait_prompt" in user_cfg:
#        SCRIPT_WAIT_PROMPT=bool(user_cfg["script_wait_prompt"])
#    if "prompt_timeout_sec" in user_cfg:
#        try:
#            vv=float(user_cfg["prompt_timeout_sec"])
#            if vv>=0: SCRIPT_PROMPT_TIMEOUT_SEC=vv
#        except: pass
#    if "fast_play_mode" in user_cfg:
#        FAST_PLAY_MODE=bool(user_cfg["fast_play_mode"])
#
#    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
#    init_baud=cfg_ini.get("BaudRate",BAUD)
#    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
#    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
#    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
#    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
#    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
#    if init_enter not in ("CR","CRLF","LF","NONE"): init_enter="CR"
#
#    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)
#
#    parity_map={"even":serial.PARITY_EVEN,"odd":serial.PARITY_ODD,"none":serial.PARITY_NONE,
#                "mark":serial.PARITY_MARK,"space":serial.PARITY_SPACE}
#    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
#    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
#    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE
#
#    if fc in ("rtscts","hard"):
#        rtscts,dsrdtr,xonxoff=True,False,False
#    elif fc=="dsrdtr":
#        rtscts,dsrdtr,xonxoff=False,True,False
#    elif fc=="x":
#        rtscts,dsrdtr,xonxoff=False,False,True
#    else:
#        rtscts=dsrdtr=xonxoff=False
#
#    char_delay=float(globals()['CHAR_DELAY_MS']); line_delay=float(globals()['LINE_DELAY_MS'])
#
#    try:
#        ser=serial.Serial(port,baud,timeout=TIMEOUT,
#                          bytesize=bytesize,parity=parity,stopbits=stopbits,
#                          rtscts=rtscts,dsrdtr=dsrdtr,xonxoff=xonxoff,write_timeout=1)
#    except serial.SerialException as e:
#        print(f"[ERR] Cannot open {port}: {e}"); return
#
#    try:
#        if ASSERT_DTR: ser.setDTR(True)
#        if ASSERT_RTS: ser.setRTS(True)
#    except Exception as e:
#        print(f"[WARN] Setting DTR/RTS failed: {e}")
#
#    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
#        try:
#            ser.reset_input_buffer(); ser.reset_output_buffer()
#        except Exception as e: print(f"[WARN] Clear buffers failed: {e}")
#
#    if not NO_BANNER:
#        print(f"[INFO] Opened {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
#        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
#        print(f"[INFO] Enter={enter_mode} char_delay={char_delay}ms line_delay={line_delay}ms script_min={SAFE_SCRIPT_CHAR_DELAY_MS}ms hex={'ON' if TX_HEX else 'OFF'} echo={'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#        print(f"[INFO] prompt_wait={'ON' if SCRIPT_WAIT_PROMPT else 'OFF'} timeout={SCRIPT_PROMPT_TIMEOUT_SEC}s fastplay={'ON' if FAST_PLAY_MODE else 'OFF'}")
#        print("[INFO] Type /help for command list.")
#
#    log_file=None
#    if LOG_PATH:
#        try:
#            log_file=open(LOG_PATH,"a",encoding="utf-8")
#            print(f"[INFO] Logging to {LOG_PATH}")
#        except Exception as e:
#            print(f"[WARN] Log open failed: {e}")
#
#    reader=SerialReaderThread(
#        ser,encoding=ENCODING,hex_dump=HEX_DUMP_RX,raw=RAW_RX,
#        log_file=log_file,quiet=QUIET_RX
#    )
#    reader.start()
#
#    send_lock=threading.Lock()
#
#    def persist_user():
#        user_cfg["char_delay_ms"]=char_delay
#        user_cfg["line_delay_ms"]=line_delay
#        user_cfg["tx_hex"]=TX_HEX
#        user_cfg["script_char_delay_ms"]=SAFE_SCRIPT_CHAR_DELAY_MS
#        user_cfg["script_local_echo"]=SCRIPT_LOCAL_ECHO
#        user_cfg["script_wait_prompt"]=SCRIPT_WAIT_PROMPT
#        user_cfg["prompt_timeout_sec"]=SCRIPT_PROMPT_TIMEOUT_SEC
#        user_cfg["fast_play_mode"]=FAST_PLAY_MODE
#        save_user_config(user_cfg)
#
#    def line_suffix():
#        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]
#
#    def send_bytes(data:bytes, tag="TX", safe=False, local_echo_line=None):
#        if not data: return
#        per_char_delay=char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
#        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
#            print(local_echo_line)
#        if per_char_delay>0 and len(data)>1:
#            for i,b in enumerate(data):
#                with send_lock:
#                    try: ser.write(bytes([b])); ser.flush()
#                    except serial.SerialException as e:
#                        print(f"[ERR] TX failed: {e}"); return
#                if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(bytes([b]))}")
#                if log_file:
#                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
#                    except: pass
#                if i<len(data)-1: time.sleep(per_char_delay/1000.0)
#        else:
#            with send_lock:
#                try: ser.write(data); ser.flush()
#                except serial.SerialException as e:
#                    print(f"[ERR] TX failed: {e}"); return
#            if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(data)}")
#            if log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
#                except: pass
#        if line_delay>0 and tag.startswith("TX"): time.sleep(line_delay/1000.0)
#
#    class ScriptContext:
#        def __init__(self):
#            self.last_prompt_seq=get_prompt_seq(); self.first_send=True
#        def wait_ready_if_needed(self):
#            if FAST_PLAY_MODE:  # skip prompt waits entirely when fastplay enabled
#                return
#            if not SCRIPT_WAIT_PROMPT: return
#            if self.first_send:
#                self.first_send=False; return
#            prev=self.last_prompt_seq
#            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
#        def note_after_send(self): pass
#
#    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        try: body=text.encode(ENCODING,errors="replace")
#        except Exception as e: print(f"[WARN] Encode failed: {e}"); return
#        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    def send_enter_only(safe=False, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        send_bytes(line_suffix(), tag="TX-EMPTY", safe=safe)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    slot_cmds={k:None for k in ALL_SLOTS}; load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
#    global_combos={}; load_global_combos(COMBO_SAVE_FILE, global_combos)
#    i2c_dump_slots={str(i):None for i in range(MAX_I2C_DUMPS)}; load_i2c_dumps(I2C_DUMP_SAVE_FILE, i2c_dump_slots)
#    cmp_history=load_cmp_history(DUMPCMP_HISTORY_FILE)
#    load_cmp_results(DUMPCMP_RESULTS_FILE)
#
#    def show_slots():
#        print("[SLOTS] ---------------------------")
#        for k in DIGIT_SLOTS+LETTER_SLOTS:
#            v=slot_cmds.get(k)
#            if v is None:
#                print(f" {k}: (empty)")
#            else:
#                t=v.get("type")
#                if t=="enter":
#                    print(f" {k}: <enter>")
#                elif t=="combo":
#                    print(f" {k}: <combo {v.get('seq','')}>")
#                else:
#                    data=v.get("data","")
#                    first=data.splitlines()[0] if data else ""
#                    more=" ..." if "\n" in data else ""
#                    print(f" {k}: {first[:60]}{more}")
#        print("[SLOTS] ---------------------------")
#
#    def show_global_combos():
#        print("[combos] (digits 0-9) -------------")
#        if not global_combos:
#            print(" (none)")
#        else:
#            for d in DIGIT_SLOTS:
#                if d in global_combos:
#                    print(f" {d}: {global_combos[d]}")
#                else:
#                    print(f" {d}: (empty)")
#        print("[combos] ---------------------------")
#
#    def dumplist():
#        print("[dumps] 0-9 stored snapshots -------")
#        for d in DIGIT_SLOTS:
#            v=i2c_dump_slots.get(d)
#            print(f" {d}: {(str(len(v))+' lines') if v else '(empty)'}")
#        print("[dumps] ---------------------------")
#
#    def dump_show(d):
#        v=i2c_dump_slots.get(d)
#        if not v:
#            print(f"[dumps] slot {d} empty"); return
#        print(f"[dumps] slot {d} ({len(v)} lines)")
#        for line in v:
#            print(line)
#
#    HEADER_LINE = "     " + " ".join(f"{i:02x}" for i in range(16))
#    ROW_ADDRS   = [f"{i:02x}" for i in range(0,256,16)]
#
#    def _parse_dump_to_matrix(lines):
#        matrix={}
#        for ln in lines:
#            if ln.startswith("#NO_HEADER#"):
#                continue
#            m=re.match(r'^([0-9A-Fa-f]{2}):\s+(.*)$', ln)
#            if not m: continue
#            addr=m.group(1).lower()
#            rest=m.group(2).strip()
#            bytes_list=[b for b in rest.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', b)]
#            if len(bytes_list)<16:
#                bytes_list += ["--"]*(16-len(bytes_list))
#            elif len(bytes_list)>16:
#                bytes_list=bytes_list[:16]
#            matrix[addr]=[b.upper() for b in bytes_list]
#        for a in ROW_ADDRS:
#            if a not in matrix:
#                matrix[a]=["--"]*16
#        return matrix
#
#    def _hex_to_bin(h):
#        try:
#            bits=f"{int(h,16):08b}"
#            return bits[:4]+"_"+bits[4:]
#        except:
#            return "----_----"
#
#    def _store_cmp_result(entry):
#        _dumpcmp_results.append(entry)
#        if len(_dumpcmp_results) > MAX_CMP_RESULTS_ENTRIES:
#            del _dumpcmp_results[:-MAX_CMP_RESULTS_ENTRIES]
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#
#    def _dump_compare_single(a,b, *, suppress_end=False):
#        da=i2c_dump_slots.get(a); db=i2c_dump_slots.get(b)
#        if not da:
#            print(f"[dumpcmp] slot {a} empty"); return None
#        if not db:
#            print(f"[dumpcmp] slot {b} empty"); return None
#        mA=_parse_dump_to_matrix(da); mB=_parse_dump_to_matrix(db)
#
#        changed_bytes=0
#        changed_rows=0
#
#        hex_lines=[]
#        bin_lines=[]
#
#        print("hex"); hex_lines.append("hex")
#        line=f" disk:{a}"; print(line); hex_lines.append(line)
#        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=[]; row_changed=False
#            for i in range(16):
#                if rowA[i]==rowB[i]:
#                    row_tokens.append("XX")
#                else:
#                    row_tokens.append(rowA[i])
#                    changed_bytes+=1
#                    row_changed=True
#            if row_changed: changed_rows+=1
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); hex_lines.append(ln)
#        line=f"disk:{b}"; print(line); hex_lines.append(line)
#        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=[]
#            for i in range(16):
#                row_tokens.append("XX" if rowA[i]==rowB[i] else rowB[i])
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); hex_lines.append(ln)
#
#        print(); bin_lines.append("")
#        print("binary"); bin_lines.append("binary")
#        line=f"disk:{a}"; print(line); bin_lines.append(line)
#        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowA[i]) for i in range(16)]
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); bin_lines.append(ln)
#        line=f"disk:{b}"; print(line); bin_lines.append(line)
#        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowB[i]) for i in range(16)]
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); bin_lines.append(ln)
#
#        if not suppress_end:
#            print("[dumpcmp] end")
#
#        entry={
#            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
#            "a": a,
#            "b": b,
#            "changed_rows": changed_rows,
#            "changed_bytes": changed_bytes,
#            "hex_lines": hex_lines,
#            "binary_lines": bin_lines
#        }
#        _store_cmp_result(entry)
#        return {"a":a,"b":b,"changed_rows":changed_rows,"changed_bytes":changed_bytes}
#
#    def dump_compare(a,b):
#        return _dump_compare_single(a,b)
#
#    def parse_multi_pairs(arg_str):
#        parts=[p.strip() for p in arg_str.split(",") if p.strip()]
#        pairs=[]
#        for p in parts:
#            toks=p.split()
#            if len(toks)!=2:
#                print(f"[dumpcmp] skip invalid pair '{p}'")
#                continue
#            da,db=toks
#            if da in DIGIT_SLOTS and db in DIGIT_SLOTS:
#                pairs.append((da,db))
#            else:
#                print(f"[dumpcmp] skip non-digit pair '{p}'")
#        return pairs
#
#    def multi_dump_compare(pairs):
#        if not pairs:
#            print("[dumpcmp] no valid pairs"); return
#        session_stats=[]
#        print(f"[dumpcmp] multi compare {len(pairs)} pair(s): {', '.join(f'{a}-{b}' for a,b in pairs)}")
#        for idx,(a,b) in enumerate(pairs,1):
#            print(f"\n[dumpcmp] pair {idx}/{len(pairs)} ({a} vs {b})")
#            stats=_dump_compare_single(a,b,suppress_end=True)
#            if stats:
#                print("[dumpcmp] end")
#                session_stats.append(stats)
#        entry={
#            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
#            "pairs":[{"a":s["a"],"b":s["b"],"changed_rows":s["changed_rows"],"changed_bytes":s["changed_bytes"]} for s in session_stats]
#        }
#        cmp_history.append(entry)
#        save_cmp_history(DUMPCMP_HISTORY_FILE,cmp_history)
#        total_changed=sum(s["changed_bytes"] for s in session_stats)
#        print(f"\n[dumpcmp] multi summary: {len(session_stats)} compared, total changed bytes={total_changed}")
#        for s in session_stats:
#            print(f"  {s['a']} vs {s['b']}: rows={s['changed_rows']} bytes={s['changed_bytes']}")
#
#    def show_cmp_history(limit=10):
#        print("[cmphist] recent multi-compare sessions:")
#        tail=cmp_history[-limit:]
#        if not tail:
#            print(" (none)"); return
#        for idx,entry in enumerate(tail,1):
#            ts=entry.get("timestamp","?")
#            pairs_txt=", ".join(f"{p['a']}-{p['b']}:{p['changed_bytes']}" for p in entry.get("pairs",[]))
#            print(f" {idx}. {ts}  {pairs_txt}")
#
#    def show_cmp_results(limit=10):
#        print("[cmpres] stored compare entries (latest first):")
#        tail=_dumpcmp_results[-limit:]
#        if not tail:
#            print(" (none)")
#            return
#        for i,entry in enumerate(tail,1):
#            print(f" {i}. {entry.get('timestamp','?')} {entry.get('a')}-{entry.get('b')} "
#                  f"bytes={entry.get('changed_bytes')} rows={entry.get('changed_rows')} "
#                  f"hex_lines={len(entry.get('hex_lines',[]))} bin_lines={len(entry.get('binary_lines',[]))}")
#
#    def clear_cmp_results():
#        global _dumpcmp_results
#        _dumpcmp_results=[]
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#        print("[cmpres] all stored pair compare results cleared.")
#
#    def show_all():
#        show_slots(); show_global_combos(); dumplist()
#
#    def print_help():
#        print("""[help]
#(all commands are lowercase)
#slots (0-9,a-z):
#  /setX <text>   /comboX <seq>  /enterX  /clrX  oX  /slots  /slotsave  /slotload
#global combos (0-9):
#  /cset d <seq>  /clist  /crun d  /cclear d  /crun_all  /csave  /cload
#i2cdump capture:
#  /dumpsave d    /dumpshow d    /dumplist
#  /dumpcmp a b
#  /dumpcmp a b,c d,e f   (multi compare pairs, alias: /dumpcmpmulti)
#  /cmphist [n]    show recent multi-compare history
#  /cmpres  [n]    list stored compare result entries
#  /cmpresclear    clear ALL stored detailed pair results (confirmation)
#timing / wait:
#  /fastplay on|off        skip prompt waits between scripted steps
#  /scriptwait on|off      enable/disable waiting for prompt pattern
#  /promptime [sec]        show/set prompt wait timeout seconds
#delays & modes:
#  /delay /scriptdelay /linedelay /hex on|off /scriptecho on|off
#general:
#  /help /quit
#hotkeys (win):
#  Ctrl+0..9 / Ctrl+a..z play slot
#  Ctrl+S show slots+combos+dumps
#  C+B+digit run digit combo
#  C+L list digit combos
#""")
#
#    def play_slot_recursive(idx_char, depth, visited, script_ctx):
#        if depth>40:
#            print("[play] depth limit"); return
#        if idx_char not in slot_cmds:
#            print(f"[play] slot {idx_char} not found"); return
#        v=slot_cmds[idx_char]
#        if v is None:
#            print(f"[play] slot {idx_char} empty"); return
#        if id(v) in visited:
#            print(f"[play] cycle at {idx_char}"); return
#        visited.add(id(v))
#        t=v.get("type")
#        if t=="enter":
#            send_enter_only(safe=True, script_ctx=script_ctx)
#        elif t=="combo":
#            for c in v.get("seq",""):
#                if c in slot_cmds:
#                    play_slot_recursive(c, depth+1, visited, script_ctx)
#        else:
#            data=v.get("data","")
#            parts=data.split(TOKEN_ENTER)
#            for pi,segment in enumerate(parts):
#                lines=segment.splitlines()
#                if not lines and segment=="":
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#                for line in lines:
#                    if line.strip()=="" and line!="":
#                        send_enter_only(safe=True, script_ctx=script_ctx)
#                    elif line!="":
#                        send_line(line,safe=True,
#                                  local_echo=f"[run] {line}" if SCRIPT_LOCAL_ECHO else None,
#                                  script_ctx=script_ctx)
#                if pi<len(parts)-1:
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#        visited.remove(id(v))
#
#    def play_slot(k):
#        if k not in slot_cmds:
#            print(f"[play] slot {k} invalid"); return
#        print(f"[play] slot {k}")
#        ctx=ScriptContext()
#        play_slot_recursive(k,0,set(),ctx)
#
#    def run_global_combo(d):
#        if d not in global_combos:
#            print(f"[combo] digit {d} undefined"); return
#        seq=global_combos[d]; print(f"[combo] run {d}: {seq}")
#        ctx=ScriptContext()
#        for c in seq:
#            if c in slot_cmds:
#                play_slot_recursive(c,0,set(),ctx)
#
#    def run_all_global_combos():
#        defined=[d for d in DIGIT_SLOTS if d in global_combos]
#        if not defined:
#            print("[combo] no digit combos defined"); return
#        print("[combo] run all digit combos:")
#        ctx=ScriptContext()
#        for d in defined:
#            seq=global_combos[d]; print(f"  -> {d}: {seq}")
#            for c in seq:
#                if c in slot_cmds:
#                    play_slot_recursive(c,0,set(),ctx)
#
#    def run_single_combo_via_hotkey(d):
#        if d in global_combos:
#            print(f"[combo] (hotkey) {d}")
#            run_global_combo(d)
#        else:
#            print(f"[combo] (hotkey) {d} undefined")
#
#    stop_hotkey=threading.Event()
#    hotkey_thread=None
#    if os.name=='nt':
#        try:
#            hotkey_thread=HotkeyThread(
#                play_callback=play_slot,
#                show_all_callback=show_all,
#                combo_list_callback=show_global_combos,
#                run_single_combo_callback=run_single_combo_via_hotkey,
#                stop_event=stop_hotkey
#            )
#            hotkey_thread.start()
#        except Exception as e:
#            print(f"[WARN] hotkey thread failed: {e}")
#
#    # Command loop
#    try:
#        while True:
#            try:
#                line=input()
#            except EOFError:
#                break
#            stripped=line.strip()
#            lower=stripped.lower()
#
#            if lower=="/help":
#                print_help(); continue
#
#            # timing / wait toggles
#            if lower.startswith("/fastplay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[fastplay] {'on' if FAST_PLAY_MODE else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        FAST_PLAY_MODE=(arg=="on")
#                        print(f"[fastplay] -> {'on' if FAST_PLAY_MODE else 'off'}")
#                        persist_user()
#                    else:
#                        print("[fastplay] use: /fastplay on|off")
#                continue
#            if lower.startswith("/scriptwait"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptwait] {'on' if SCRIPT_WAIT_PROMPT else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        SCRIPT_WAIT_PROMPT=(arg=="on")
#                        print(f"[scriptwait] -> {'on' if SCRIPT_WAIT_PROMPT else 'off'}")
#                        persist_user()
#                    else:
#                        print("[scriptwait] use: /scriptwait on|off")
#                continue
#            if lower.startswith("/promptime"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[promptime] {SCRIPT_PROMPT_TIMEOUT_SEC} sec")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        SCRIPT_PROMPT_TIMEOUT_SEC=v
#                        print(f"[promptime] -> {SCRIPT_PROMPT_TIMEOUT_SEC} sec")
#                        persist_user()
#                    except:
#                        print(f"[promptime] invalid: {parts[1]}")
#                continue
#
#            # i2c dump commands
#            if lower.startswith("/dumpsave"):
#                parts=lower.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[dumps] usage: /dumpsave <digit>")
#                else:
#                    d=parts[1]
#                    if _last_captured_dump:
#                        i2c_dump_slots[d]=_last_captured_dump[:]
#                        print(f"[dumps] saved capture to slot {d} ({len(_last_captured_dump)} lines)")
#                        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#                    else:
#                        print("[dumps] no captured dump to save")
#                continue
#
#            if lower.startswith("/dumpshow"):
#                parts=lower.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[dumps] usage: /dumpshow <digit>")
#                else:
#                    dump_show(parts[1])
#                continue
#
#            if lower=="/dumplist":
#                dumplist(); continue
#
#            # dumpcmp (single/multi)
#            if lower.startswith("/dumpcmp") or lower.startswith("/dumpcmpmulti"):
#                cmd,*rest=lower.split(None,1)
#                if not rest:
#                    print("[dumpcmp] usage: /dumpcmp a b  OR /dumpcmp a b,c d")
#                    continue
#                rem=rest[0].strip()
#                if ',' in rem or len(rem.split())>2:
#                    if ',' not in rem:
#                        toks=rem.split()
#                        if len(toks)>=4 and len(toks)%2==0:
#                            pair_strs=[f"{toks[i]} {toks[i+1]}" for i in range(0,len(toks),2)]
#                            rem=",".join(pair_strs)
#                    pairs=parse_multi_pairs(rem)
#                    multi_dump_compare(pairs)
#                else:
#                    parts=rem.split()
#                    if len(parts)!=2 or parts[0] not in DIGIT_SLOTS or parts[1] not in DIGIT_SLOTS:
#                        print("[dumpcmp] usage: /dumpcmp <a> <b>")
#                    else:
#                        dump_compare(parts[0],parts[1])
#                continue
#
#            if lower.startswith("/cmphist"):
#                parts=lower.split()
#                limit=10
#                if len(parts)==2 and parts[1].isdigit():
#                    limit=max(1,min(100,int(parts[1])))
#                show_cmp_history(limit)
#                continue
#
#            if lower.startswith("/cmpresclear"):
#                confirm=input("type yes to confirm clearing all stored compare results: ").strip().lower()
#                if confirm=="yes":
#                    clear_cmp_results()
#                else:
#                    print("[cmpres] cancelled.")
#                continue
#
#            if lower.startswith("/cmpres"):
#                parts=lower.split()
#                limit=10
#                if len(parts)==2 and parts[1].isdigit():
#                    limit=max(1,min(200,int(parts[1])))
#                show_cmp_results(limit)
#                continue
#
#            # Delays & modes
#            if lower.startswith("/delay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[delay] {char_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        char_delay=v; print(f"[delay] -> {char_delay} ms"); persist_user()
#                    except: print(f"[delay] invalid: {parts[1]}")
#                continue
#
#            if lower.startswith("/scriptdelay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptdelay] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[scriptdelay] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
#                    except: print(f"[scriptdelay] invalid: {parts[1]}")
#                continue
#
#            if lower.startswith("/linedelay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[linedelay] {line_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        line_delay=v; print(f"[linedelay] -> {line_delay} ms"); persist_user()
#                    except: print(f"[linedelay] invalid: {parts[1]}")
#                continue
#
#            if lower.startswith("/scriptecho"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptecho] {'on' if SCRIPT_LOCAL_ECHO else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        SCRIPT_LOCAL_ECHO=(arg=="on")
#                        print(f"[scriptecho] -> {'on' if SCRIPT_LOCAL_ECHO else 'off'}"); persist_user()
#                    else:
#                        print("[scriptecho] use: /scriptecho on|off")
#                continue
#
#            if lower.startswith("/hex"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[hex] {'on' if TX_HEX else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        TX_HEX=(arg=="on")
#                        print(f"[hex] -> {'on' if TX_HEX else 'off'}"); persist_user()
#                    else:
#                        print("[hex] use: /hex on|off")
#                continue
#
#            # Slots persistence
#            if lower=="/slotsave":
#                save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
#            if lower=="/slotload":
#                load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue
#
#            # Combos
#            if lower=="/clist":
#                show_global_combos(); continue
#            if lower.startswith("/cset "):
#                parts=stripped.split(None,2)
#                parts_l=lower.split(None,2)
#                if len(parts_l)<3:
#                    print("[combo] usage: /cset <digit> <seq>")
#                else:
#                    digit=parts_l[1]
#                    if not (digit.isdigit() and len(digit)==1):
#                        print("[combo] name must be single digit (0-9)")
#                    else:
#                        seq="".join(ch for ch in parts_l[2] if ch.isalnum())
#                        global_combos[digit]=seq
#                        print(f"[combo] {digit} = {seq}")
#                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                continue
#            if lower.startswith("/crun "):
#                digit=lower.split(None,1)[1].strip()
#                run_global_combo(digit); continue
#            if lower.startswith("/cclear "):
#                digit=lower.split(None,1)[1].strip()
#                if digit in global_combos:
#                    del global_combos[digit]; print(f"[combo] cleared {digit}")
#                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                else:
#                    print(f"[combo] {digit} not defined")
#                continue
#            if lower=="/csave":
#                save_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if lower=="/cload":
#                load_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if lower=="/crun_all":
#                run_all_global_combos(); continue
#
#            # General
#            if lower=="/quit":
#                print("[info] quit")
#                break
#            if lower=="/slots":
#                show_slots(); continue
#
#            # Slot definitions (/enterx /combox /setx /clrx)
#            if lower.startswith("/enter") and len(lower)==7:
#                key=lower[6]
#                if key in slot_cmds:
#                    slot_cmds[key]={"type":"enter"}
#                    print(f"[set] slot {key} = <enter>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/combo") and len(lower)>=7:
#                key=lower[6]
#                if key in slot_cmds:
#                    parts=stripped.split(None,1)
#                    seq=""
#                    if len(parts)>1:
#                        seq="".join(ch for ch in parts[1] if ch.isalnum())
#                    slot_cmds[key]={"type":"combo","seq":seq}
#                    print(f"[set] slot {key} = <combo {seq}>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/set") and len(lower)>=5:
#                key=lower[4]
#                if key in slot_cmds:
#                    parts=stripped.split(None,1)
#                    data=parts[1] if len(parts)>1 else ""
#                    slot_cmds[key]={"type":"raw","data":data}
#                    print(f"[set] slot {key} raw length={len(data)}")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/clr") and len(lower)==5:
#                key=lower[4]
#                if key in slot_cmds:
#                    slot_cmds[key]=None
#                    print(f"[clr] slot {key} cleared")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            # Play slot (ox)
#            if len(lower)==2 and lower[0]=='o':
#                key=lower[1]
#                if key in slot_cmds:
#                    play_slot(key)
#                continue
#
#            # Blank line -> ENTER
#            if line=="":
#                send_enter_only(safe=False)
#                continue
#
#            # Normal input
#            try:
#                body=line.encode(ENCODING,errors="replace")
#            except Exception as e:
#                print(f"[WARN] encode failed: {e}")
#                continue
#            send_bytes(body+line_suffix(), safe=False, tag="TX")
#
#    except KeyboardInterrupt:
#        print("\n[INFO] keyboardinterrupt")
#    finally:
#        persist_user()
#        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#        save_cmp_history(DUMPCMP_HISTORY_FILE, cmp_history)
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#        if 'hotkey_thread' in locals() and hotkey_thread:
#            stop_hotkey.set()
#            hotkey_thread.join(timeout=0.5)
#        reader.stop()
#        time.sleep(0.05)
#        try: ser.close()
#        except: pass
#        if 'log_file' in locals() and log_file:
#            try: log_file.close()
#            except: pass
#        print("[INFO] exit")
#
#if __name__ == "__main__":
#    main()
#
#    """




















    













    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
250904_0001_i2cdump_data_compare_pass
250904_0002_i2cdump_data_compare_binary_pass
250904_0003_i2cdump_data_multiple_compare_pass
250904_0004_i2cdump_data_multiple_compare_save_pass

    """


#    """
##!/usr/bin/env python
## -*- coding: utf-8 -*-
#"""
#Serial console with:
# - Slots 0-9 + a-z (36 total)
# - Digit global combos (0-9)
# - Hotkeys: Ctrl+0..9 / Ctrl+a..z (play slot), Ctrl+S (show all), C+B+<digit> (single combo), C+L (list combos)
# - i2cdump capture & storage (/dumpsave /dumpshow /dumplist /dumpcmp)
# - Tolerant i2cdump capture (header or first data row, prompt line, overflow guard)
# - /dumpcmp:
#     hex
#       disk:<a> (unchanged => XX, changed => HEX from A)
#       disk:<b> (unchanged => XX, changed => HEX from B)
#     binary
#       disk:<a> row lines (unchanged => XX, changed => 8-bit binary xxxx_xxxx)
#       disk:<b> row lines (same rule)
#     (Binary section prints rows once per dump with changed bytes shown inline.)
# - Multi-compare support:
#     /dumpcmp 1 2,2 3,3 4  (sequential pairs)
#     /dumpcmpmulti alias
#     History summary stored in .dumpcmp_history.json
# - Detailed pair results stored in .dumpcmp_results.json (every pair, single or multi)
#   Entry fields: timestamp, a, b, changed_rows, changed_bytes, hex_lines[], binary_lines[]
# - Commands (ALL LOWERCASE NOW):
#     /cmphist [n]      show multi-compare history
#     /cmpres  [n]      list recent stored pair results (metadata)
#     /cmpresclear      clear all stored detailed pair compare results (confirmation)
# - Receiver thread style preserved (only feed hook used, not altered)
#
#All command parsing expects lowercase now.
#"""
#
#import sys
#import serial
#import threading
#import time
#import os
#import json
#import re
#from datetime import datetime
#
#try:
#    from serial.tools import list_ports
#except ImportError:
#    list_ports = None
#
## ================== Config (overridden by saved user config) ==================
#PORT                    = "COM5"
#BAUD                    = 115200
#PARITY_NAME             = "none"
#DATA_BITS               = 8
#STOP_BITS               = 1
#FLOW_CTRL               = "none"
#ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
#ENCODING                = "utf-8"
#TIMEOUT                 = 0.05
#CHAR_DELAY_MS           = 0
#LINE_DELAY_MS           = 0
#ASSERT_DTR              = False
#ASSERT_RTS              = False
#CLEAR_BUFF_ON_OPEN      = False
#
#TX_HEX                  = True
#HEX_DUMP_RX             = False
#RAW_RX                  = False
#QUIET_RX                = False
#
#LOG_PATH                = None
#INI_PATH                = None
#NO_BANNER               = False
#
#INTERACTIVE_SELECT      = True
#REMEMBER_LAST           = True
#LAST_FILE_NAME          = ".last_port"
#
#SLOTS_SAVE_FILE         = ".slot_cmds.json"
#AUTO_SAVE_SLOTS         = True
#SHOW_SAVE_MESSAGE       = True
#
#COMBO_SAVE_FILE         = ".combo_defs.json"
#AUTO_SAVE_COMBOS        = True
#SHOW_COMBO_SAVE_MSG     = True
#
#USER_CONFIG_FILE        = ".console_config.json"
#AUTO_SAVE_CONFIG        = True
#
#I2C_DUMP_SAVE_FILE      = ".i2c_dumps.json"
#AUTO_SAVE_I2C_DUMPS     = True
#MAX_I2C_DUMPS           = 10   # 0-9
#
#SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
#SCRIPT_LOCAL_ECHO         = False
#
#PROMPT_PATTERN            = "i2c>"
#SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
#SCRIPT_WAIT_PROMPT        = True
#POST_PROMPT_STABILIZE_MS  = 5
#
#HOTKEY_POLL_INTERVAL_SEC  = 0.05
#TOKEN_ENTER               = "<ENTER>"
#
#DIGIT_SLOTS  = [str(i) for i in range(10)]
#LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
#ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS
#
#DUMPCMP_HISTORY_FILE     = ".dumpcmp_history.json"
#MAX_CMP_HISTORY_ENTRIES  = 200
#
## Detailed results storage
#DUMPCMP_RESULTS_FILE     = ".dumpcmp_results.json"
#MAX_CMP_RESULTS_ENTRIES  = 400
#_dumpcmp_results         = []
#
## ======================================================================
## Utility
## ======================================================================
#def format_hex(data: bytes) -> str:
#    return " ".join(f"{b:02X}" for b in data)
#
#def parse_ini(path: str):
#    out = {}
#    if not path or not os.path.isfile(path):
#        return out
#    try:
#        with open(path, "r", encoding="utf-8", errors="ignore") as f:
#            for line in f:
#                line=line.strip()
#                if not line or line.startswith(";") or "=" not in line:
#                    continue
#                k,v=line.split("=",1)
#                k=k.strip(); v=v.strip()
#                kl=k.lower()
#                if kl in ("comport","baudrate","delayperchar","delayperline"):
#                    try: out[k]=int(v)
#                    except: pass
#                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
#                    out[k]=v
#    except Exception as e:
#        print(f"[WARN] INI parse failed: {e}")
#    return out
#
#def load_user_config():
#    if not os.path.isfile(USER_CONFIG_FILE):
#        return {}
#    try:
#        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        return data if isinstance(data,dict) else {}
#    except Exception as e:
#        print(f"[CFG] Load failed: {e}")
#        return {}
#
#def save_user_config(cfg):
#    if not AUTO_SAVE_CONFIG: return
#    try:
#        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
#            json.dump(cfg,f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[CFG] Save failed: {e}")
#
#def normalize_slot_value(v):
#    if v is None: return None
#    if isinstance(v,dict):
#        t=v.get("type")
#        if t=="raw":
#            d=v.get("data"); return {"type":"raw","data": d if isinstance(d,str) else ""}
#        if t=="enter": return {"type":"enter"}
#        if t=="combo":
#            seq=v.get("seq","")
#            if not isinstance(seq,str): seq=""
#            return {"type":"combo","seq":seq}
#        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
#    if isinstance(v,str): return {"type":"raw","data":v}
#    return {"type":"raw","data":str(v)}
#
#def load_slots_from_file(path, slot_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        changed=False
#        for k in slot_dict.keys():
#            if k in data:
#                slot_dict[k]=normalize_slot_value(data[k]); changed=True
#        if changed: print(f"[SLOTS] Loaded {path}")
#    except Exception as e:
#        print(f"[SLOTS] Load failed: {e}")
#
#def save_slots_to_file(path, slot_dict):
#    try:
#        out={k:(None if v is None else v) for k,v in slot_dict.items()}
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(out,f,ensure_ascii=False,indent=2)
#        print(f"[SLOTS] Saved -> {path}")
#    except Exception as e:
#        print(f"[SLOTS] Save failed: {e}")
#
#def load_global_combos(path, combo_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
#                    combo_dict[k]="".join(ch for ch in v if ch.isalnum())
#            print(f"[COMBO] Loaded {path} ({len(combo_dict)} items)")
#    except Exception as e:
#        print(f"[COMBO] Load failed: {e}")
#
#def save_global_combos(path, combo_dict):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
#        print(f"[COMBO] Saved -> {path}")
#    except Exception as e:
#        print(f"[COMBO] Save failed: {e}")
#
#def load_i2c_dumps(path, dump_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if k in dump_dict and isinstance(v,list):
#                    dump_dict[k]=v
#        print(f"[DUMPS] Loaded {path}")
#    except Exception as e:
#        print(f"[DUMPS] Load failed: {e}")
#
#def save_i2c_dumps(path, dump_dict):
#    if not AUTO_SAVE_I2C_DUMPS: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(dump_dict,f,ensure_ascii=False,indent=2)
#        print(f"[DUMPS] Saved -> {path}")
#    except Exception as e:
#        print(f"[DUMPS] Save failed: {e}")
#
#def load_cmp_history(path):
#    if not os.path.isfile(path): return []
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,list): return data
#    except Exception as e:
#        print(f"[CMPHIST] Load failed: {e}")
#    return []
#
#def save_cmp_history(path, hist):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(hist[-MAX_CMP_HISTORY_ENTRIES:],f,ensure_ascii=False,indent=2)
#        print(f"[CMPHIST] Saved -> {path}")
#    except Exception as e:
#        print(f"[CMPHIST] Save failed: {e}")
#
#def load_cmp_results(path):
#    global _dumpcmp_results
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,list):
#            _dumpcmp_results=data
#            print(f"[DUMPCMP] Loaded {len(_dumpcmp_results)} stored compare results")
#    except Exception as e:
#        print(f"[DUMPCMP] Results load failed: {e}")
#
#def save_cmp_results(path):
#    if not _dumpcmp_results: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(_dumpcmp_results[-MAX_CMP_RESULTS_ENTRIES:],f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[DUMPCMP] Results save failed: {e}")
#
## ======================================================================
## Prompt tracking
## ======================================================================
#prompt_lock=threading.Lock()
#prompt_seq=0
#def inc_prompt_if_in(text:str):
#    global prompt_seq
#    if PROMPT_PATTERN and PROMPT_PATTERN in text:
#        with prompt_lock:
#            prompt_seq+=1
#def get_prompt_seq():
#    with prompt_lock:
#        return prompt_seq
#def wait_for_next_prompt(prev_seq, timeout):
#    if not SCRIPT_WAIT_PROMPT: return prev_seq
#    deadline=time.time()+timeout
#    while time.time()<deadline:
#        cur=get_prompt_seq()
#        if cur>prev_seq:
#            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
#            return cur
#        time.sleep(0.01)
#    return get_prompt_seq()
#
## ======================================================================
## i2cdump capture logic
## ======================================================================
#_i2c_capture_buffer_fragment=""
#_i2c_capture_active=False
#_i2c_capture_lines=[]
#_last_captured_dump=None
#
#_I2C_HEADER_RE = re.compile(r'^\s+00(?:\s+[0-9A-Fa-f]{2}){15}\s*$')
#_I2C_DATA_ROW_RE = re.compile(r'^[0-9A-Fa-f]{2}:\s+([0-9A-Fa-f]{2}\s+){0,15}[0-9A-Fa-f]{2}\s*$')
#_LAST_ADDR = "f0"
#
#def _maybe_finalize_partial(reason:str):
#    global _i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if _i2c_capture_active and _i2c_capture_lines:
#        _last_captured_dump=_i2c_capture_lines[:]
#        print(f"\n[DUMPS] Captured ({reason}) {len(_last_captured_dump)} lines")
#    _i2c_capture_active=False
#    _i2c_capture_lines=[]
#
#def _i2c_capture_feed(chunk:str):
#    global _i2c_capture_buffer_fragment,_i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if not chunk: return
#    _i2c_capture_buffer_fragment += chunk
#    while True:
#        if '\n' not in _i2c_capture_buffer_fragment:
#            break
#        line,rest=_i2c_capture_buffer_fragment.split('\n',1)
#        _i2c_capture_buffer_fragment=rest
#        line=line.rstrip('\r')
#        if PROMPT_PATTERN and line.startswith(PROMPT_PATTERN):
#            if _i2c_capture_active:
#                _maybe_finalize_partial("prompt")
#            continue
#        if not _i2c_capture_active:
#            if _I2C_HEADER_RE.match(line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=[line]
#                continue
#            if re.match(r'^00:\s', line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=["#NO_HEADER#"]
#            else:
#                continue
#        if _i2c_capture_active:
#            if _I2C_DATA_ROW_RE.match(line) or line==_i2c_capture_lines[0]:
#                if line != _i2c_capture_lines[0]:
#                    _i2c_capture_lines.append(line)
#            else:
#                if line.strip():
#                    _i2c_capture_lines.append(line)
#            if line.lower().startswith(_LAST_ADDR + ":"):
#                _last_captured_dump=_i2c_capture_lines[:]
#                print(f"\n[DUMPS] Captured i2cdump ({len(_last_captured_dump)} lines)")
#                _i2c_capture_active=False
#                _i2c_capture_lines=[]
#                continue
#            if len(_i2c_capture_lines) > 60:
#                _maybe_finalize_partial("overflow")
#                continue
#
## ======================================================================
## Receiver thread (unchanged logic)
## ======================================================================
#class SerialReaderThread(threading.Thread):
#    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
#        super().__init__(daemon=True)
#        self.ser=ser; self.encoding=encoding
#        self.hex_dump=hex_dump; self.raw=raw
#        self.log_file=log_file; self.quiet=quiet
#        self._running=True
#    def stop(self): self._running=False
#    def run(self):
#        while self._running and self.ser.is_open:
#            try:
#                data=self.ser.read(self.ser.in_waiting or 1)
#            except serial.SerialException as e:
#                print(f"[ERR] Serial exception: {e}")
#                break
#            if not data: continue
#            if self.log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try:
#                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n"); self.log_file.flush()
#                except Exception: pass
#            if self.quiet: continue
#            if self.hex_dump:
#                txt=format_hex(data)
#                print(f"[RX HEX] {txt}")
#                inc_prompt_if_in(txt)
#                _i2c_capture_feed(txt+"\n")
#            elif self.raw:
#                sys.stdout.buffer.write(data); sys.stdout.flush()
#                try:
#                    decoded=data.decode(self.encoding,errors="ignore")
#                    inc_prompt_if_in(decoded)
#                    _i2c_capture_feed(decoded)
#                except: pass
#            else:
#                try:
#                    text=data.decode(self.encoding,errors="replace")
#                except Exception:
#                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
#                print(text,end="",flush=True)
#                inc_prompt_if_in(text)
#                _i2c_capture_feed(text)
#
## ======================================================================
## Port selection
## ======================================================================
#def load_last_port():
#    if not REMEMBER_LAST: return None
#    try:
#        if os.path.isfile(LAST_FILE_NAME):
#            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
#                v=f.read().strip()
#                if v: return v
#    except: pass
#    return None
#def save_last_port(p):
#    if not REMEMBER_LAST: return
#    try:
#        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
#            f.write(p.strip())
#    except: pass
#
#def interactive_select_port(default_port):
#    port=default_port; baud=BAUD; parity_name=PARITY_NAME
#    data_bits=DATA_BITS; stop_bits=STOP_BITS; flow_ctrl=FLOW_CTRL; enter_mode=ENTER_MODE
#    last=load_last_port()
#    if last: default_port=last
#    if not INTERACTIVE_SELECT:
#        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#    print("=== Serial Interactive Config (Enter to keep default) ===")
#    if list_ports:
#        ports=list(list_ports.comports())
#        if ports:
#            print("Available ports:")
#            for idx,p in enumerate(ports,1):
#                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
#        else:
#            print("No COM ports detected.")
#    val=input(f"Port [{default_port}]: ").strip()
#    if val: port=val
#    val=input(f"Baud [{baud}]: ").strip()
#    if val.isdigit(): baud=int(val)
#    plist=["none","even","odd","mark","space"]
#    val=input(f"Parity {plist} [{parity_name}]: ").strip().lower()
#    if val in plist: parity_name=val
#    val=input(f"Data bits (7/8) [{data_bits}]: ").strip()
#    if val in ("7","8"): data_bits=int(val)
#    val=input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
#    if val in ("1","2"): stop_bits=int(val)
#    flist=["none","rtscts","dsrdtr","x"]
#    val=input(f"flowctrl {flist} [{flow_ctrl}]: ").strip().lower()
#    if val in flist: flow_ctrl=val
#    emlist=["CR","CRLF","LF","NONE"]
#    val=input(f"enter mode {emlist} [{enter_mode}]: ").strip().upper()
#    if val in emlist: enter_mode=val
#    save_last_port(port)
#    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#
## ======================================================================
## Hotkey Thread
## ======================================================================
#class HotkeyThread(threading.Thread):
#    def __init__(self,
#                 play_callback,
#                 show_all_callback,
#                 combo_list_callback,
#                 run_single_combo_callback,
#                 stop_event):
#        super().__init__(daemon=True)
#        self.play_callback=play_callback
#        self.show_all_callback=show_all_callback
#        self.combo_list_callback=combo_list_callback
#        self.run_single_combo_callback=run_single_combo_callback
#        self.stop_event=stop_event
#        import ctypes
#        self.ctypes=ctypes
#        self.user32=ctypes.WinDLL("user32", use_last_error=True)
#        self.VK_CTRL=0x11; self.VK_S=0x53
#        self.VK_C=0x43; self.VK_B=0x42; self.VK_L=0x4C
#        self.VK_0_9=list(range(0x30,0x3A))
#        self.VK_NUM_0_9=list(range(0x60,0x6A))
#        self.VK_A_Z=list(range(0x41,0x5B))
#        self.prev_digit_down={vk:False for vk in self.VK_0_9+self.VK_NUM_0_9}
#        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
#        self.prev_s_down=False
#        self.prev_cb=False
#        self.prev_cl_combo_list=False
#    def key_down(self,vk):
#        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
#    def run(self):
#        while not self.stop_event.is_set():
#            ctrl=self.key_down(self.VK_CTRL)
#            s_now=ctrl and self.key_down(self.VK_S)
#            if s_now and not self.prev_s_down:
#                print(); self.show_all_callback()
#            self.prev_s_down=s_now
#            c_now=self.key_down(self.VK_C); b_now=self.key_down(self.VK_B)
#            cb_now=c_now and b_now
#            if cb_now:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        self.run_single_combo_callback(key)
#                    self.prev_digit_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#            l_now=self.key_down(self.VK_L); cl_now=c_now and l_now
#            if cl_now and not self.prev_cl_combo_list:
#                print(); self.combo_list_callback()
#            self.prev_cl_combo_list=cl_now
#            if ctrl:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        print(); self.play_callback(key.lower())
#                    self.prev_digit_down[vk]=now
#                for vk in self.VK_A_Z:
#                    now=self.key_down(vk)
#                    if now and not self.prev_letter_down[vk]:
#                        key=chr(vk).lower()
#                        print(); self.play_callback(key)
#                    self.prev_letter_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
#                self.prev_s_down=False
#            time.sleep(HOTKEY_POLL_INTERVAL_SEC)
#
## ======================================================================
## Main
## ======================================================================
#def main():
#    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO, _last_captured_dump
#    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
#    user_cfg=load_user_config()
#
#    if "char_delay_ms" in user_cfg:
#        try: globals()['CHAR_DELAY_MS']=float(user_cfg["char_delay_ms"])
#        except: pass
#    if "line_delay_ms" in user_cfg:
#        try: globals()['LINE_DELAY_MS']=float(user_cfg["line_delay_ms"])
#        except: pass
#    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
#    if "script_char_delay_ms" in user_cfg:
#        try:
#            v=float(user_cfg["script_char_delay_ms"])
#            if v>=0: SAFE_SCRIPT_CHAR_DELAY_MS=v
#        except: pass
#    if "script_local_echo" in user_cfg:
#        SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
#
#    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
#    init_baud=cfg_ini.get("BaudRate",BAUD)
#    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
#    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
#    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
#    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
#    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
#    if init_enter not in ("CR","CRLF","LF","NONE"): init_enter="CR"
#
#    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)
#
#    parity_map={"even":serial.PARITY_EVEN,"odd":serial.PARITY_ODD,"none":serial.PARITY_NONE,
#                "mark":serial.PARITY_MARK,"space":serial.PARITY_SPACE}
#    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
#    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
#    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE
#
#    if fc in ("rtscts","hard"):
#        rtscts,dsrdtr,xonxoff=True,False,False
#    elif fc=="dsrdtr":
#        rtscts,dsrdtr,xonxoff=False,True,False
#    elif fc=="x":
#        rtscts,dsrdtr,xonxoff=False,False,True
#    else:
#        rtscts=dsrdtr=xonxoff=False
#
#    char_delay=float(globals()['CHAR_DELAY_MS']); line_delay=float(globals()['LINE_DELAY_MS'])
#
#    try:
#        ser=serial.Serial(port,baud,timeout=TIMEOUT,
#                          bytesize=bytesize,parity=parity,stopbits=stopbits,
#                          rtscts=rtscts,dsrdtr=dsrdtr,xonxoff=xonxoff,write_timeout=1)
#    except serial.SerialException as e:
#        print(f"[ERR] Cannot open {port}: {e}"); return
#
#    try:
#        if ASSERT_DTR: ser.setDTR(True)
#        if ASSERT_RTS: ser.setRTS(True)
#    except Exception as e:
#        print(f"[WARN] Setting DTR/RTS failed: {e}")
#
#    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
#        try:
#            ser.reset_input_buffer(); ser.reset_output_buffer()
#        except Exception as e: print(f"[WARN] Clear buffers failed: {e}")
#
#    if not NO_BANNER:
#        print(f"[INFO] Opened {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
#        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
#        print(f"[INFO] Enter={enter_mode} char_delay={char_delay}ms line_delay={line_delay}ms script_min={SAFE_SCRIPT_CHAR_DELAY_MS}ms hex={'ON' if TX_HEX else 'OFF'} echo={'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#        print("[INFO] Type /help for command list.")
#
#    log_file=None
#    if LOG_PATH:
#        try:
#            log_file=open(LOG_PATH,"a",encoding="utf-8")
#            print(f"[INFO] Logging to {LOG_PATH}")
#        except Exception as e:
#            print(f"[WARN] Log open failed: {e}")
#
#    reader=SerialReaderThread(
#        ser,encoding=ENCODING,hex_dump=HEX_DUMP_RX,raw=RAW_RX,
#        log_file=log_file,quiet=QUIET_RX
#    )
#    reader.start()
#
#    send_lock=threading.Lock()
#
#    def persist_user():
#        user_cfg["char_delay_ms"]=char_delay
#        user_cfg["line_delay_ms"]=line_delay
#        user_cfg["tx_hex"]=TX_HEX
#        user_cfg["script_char_delay_ms"]=SAFE_SCRIPT_CHAR_DELAY_MS
#        user_cfg["script_local_echo"]=SCRIPT_LOCAL_ECHO
#        save_user_config(user_cfg)
#
#    def line_suffix():
#        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]
#
#    def send_bytes(data:bytes, tag="TX", safe=False, local_echo_line=None):
#        if not data: return
#        per_char_delay=char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
#        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
#            print(local_echo_line)
#        if per_char_delay>0 and len(data)>1:
#            for i,b in enumerate(data):
#                with send_lock:
#                    try: ser.write(bytes([b])); ser.flush()
#                    except serial.SerialException as e:
#                        print(f"[ERR] TX failed: {e}"); return
#                if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(bytes([b]))}")
#                if log_file:
#                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
#                    except: pass
#                if i<len(data)-1: time.sleep(per_char_delay/1000.0)
#        else:
#            with send_lock:
#                try: ser.write(data); ser.flush()
#                except serial.SerialException as e:
#                    print(f"[ERR] TX failed: {e}"); return
#            if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(data)}")
#            if log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
#                except: pass
#        if line_delay>0 and tag.startswith("TX"): time.sleep(line_delay/1000.0)
#
#    class ScriptContext:
#        def __init__(self):
#            self.last_prompt_seq=get_prompt_seq(); self.first_send=True
#        def wait_ready_if_needed(self):
#            if not SCRIPT_WAIT_PROMPT: return
#            if self.first_send:
#                self.first_send=False; return
#            prev=self.last_prompt_seq
#            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
#        def note_after_send(self): pass
#
#    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        try: body=text.encode(ENCODING,errors="replace")
#        except Exception as e: print(f"[WARN] Encode failed: {e}"); return
#        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    def send_enter_only(safe=False, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        send_bytes(line_suffix(), tag="TX-EMPTY", safe=safe)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    slot_cmds={k:None for k in ALL_SLOTS}; load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
#    global_combos={}; load_global_combos(COMBO_SAVE_FILE, global_combos)
#    i2c_dump_slots={str(i):None for i in range(MAX_I2C_DUMPS)}; load_i2c_dumps(I2C_DUMP_SAVE_FILE, i2c_dump_slots)
#    cmp_history=load_cmp_history(DUMPCMP_HISTORY_FILE)
#    load_cmp_results(DUMPCMP_RESULTS_FILE)
#
#    def show_slots():
#        print("[SLOTS] ---------------------------")
#        for k in DIGIT_SLOTS+LETTER_SLOTS:
#            v=slot_cmds.get(k)
#            if v is None:
#                print(f" {k}: (empty)")
#            else:
#                t=v.get("type")
#                if t=="enter":
#                    print(f" {k}: <enter>")
#                elif t=="combo":
#                    print(f" {k}: <combo {v.get('seq','')}>")
#                else:
#                    data=v.get("data","")
#                    first=data.splitlines()[0] if data else ""
#                    more=" ..." if "\n" in data else ""
#                    print(f" {k}: {first[:60]}{more}")
#        print("[SLOTS] ---------------------------")
#
#    def show_global_combos():
#        print("[combos] (digits 0-9) -------------")
#        if not global_combos:
#            print(" (none)")
#        else:
#            for d in DIGIT_SLOTS:
#                if d in global_combos:
#                    print(f" {d}: {global_combos[d]}")
#                else:
#                    print(f" {d}: (empty)")
#        print("[combos] ---------------------------")
#
#    def dumplist():
#        print("[dumps] 0-9 stored snapshots -------")
#        for d in DIGIT_SLOTS:
#            v=i2c_dump_slots.get(d)
#            print(f" {d}: {(str(len(v))+' lines') if v else '(empty)'}")
#        print("[dumps] ---------------------------")
#
#    def dump_show(d):
#        v=i2c_dump_slots.get(d)
#        if not v:
#            print(f"[dumps] slot {d} empty"); return
#        print(f"[dumps] slot {d} ({len(v)} lines)")
#        for line in v:
#            print(line)
#
#    HEADER_LINE = "     " + " ".join(f"{i:02x}" for i in range(16))
#    ROW_ADDRS   = [f"{i:02x}" for i in range(0,256,16)]
#
#    def _parse_dump_to_matrix(lines):
#        matrix={}
#        for ln in lines:
#            if ln.startswith("#NO_HEADER#"):
#                continue
#            m=re.match(r'^([0-9A-Fa-f]{2}):\s+(.*)$', ln)
#            if not m: continue
#            addr=m.group(1).lower()
#            rest=m.group(2).strip()
#            bytes_list=[b for b in rest.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', b)]
#            if len(bytes_list)<16:
#                bytes_list += ["--"]*(16-len(bytes_list))
#            elif len(bytes_list)>16:
#                bytes_list=bytes_list[:16]
#            matrix[addr]=[b.upper() for b in bytes_list]
#        for a in ROW_ADDRS:
#            if a not in matrix:
#                matrix[a]=["--"]*16
#        return matrix
#
#    def _hex_to_bin(h):
#        try:
#            bits=f"{int(h,16):08b}"
#            return bits[:4]+"_"+bits[4:]
#        except:
#            return "----_----"
#
#    def _store_cmp_result(entry):
#        _dumpcmp_results.append(entry)
#        if len(_dumpcmp_results) > MAX_CMP_RESULTS_ENTRIES:
#            del _dumpcmp_results[:-MAX_CMP_RESULTS_ENTRIES]
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#
#    def _dump_compare_single(a,b, *, suppress_end=False):
#        da=i2c_dump_slots.get(a); db=i2c_dump_slots.get(b)
#        if not da:
#            print(f"[dumpcmp] slot {a} empty"); return None
#        if not db:
#            print(f"[dumpcmp] slot {b} empty"); return None
#        mA=_parse_dump_to_matrix(da); mB=_parse_dump_to_matrix(db)
#
#        changed_bytes=0
#        changed_rows=0
#
#        hex_lines=[]
#        bin_lines=[]
#
#        print("hex"); hex_lines.append("hex")
#        line=f" disk:{a}"; print(line); hex_lines.append(line)
#        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=[]; row_changed=False
#            for i in range(16):
#                if rowA[i]==rowB[i]:
#                    row_tokens.append("XX")
#                else:
#                    row_tokens.append(rowA[i])
#                    changed_bytes+=1
#                    row_changed=True
#            if row_changed: changed_rows+=1
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); hex_lines.append(ln)
#        line=f"disk:{b}"; print(line); hex_lines.append(line)
#        print(HEADER_LINE); hex_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=[]
#            for i in range(16):
#                row_tokens.append("XX" if rowA[i]==rowB[i] else rowB[i])
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); hex_lines.append(ln)
#
#        print(); bin_lines.append("")
#        print("binary"); bin_lines.append("binary")
#        line=f"disk:{a}"; print(line); bin_lines.append(line)
#        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowA[i]) for i in range(16)]
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); bin_lines.append(ln)
#        line=f"disk:{b}"; print(line); bin_lines.append(line)
#        print(HEADER_LINE); bin_lines.append(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            row_tokens=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowB[i]) for i in range(16)]
#            ln=f"{addr}:  {' '.join(row_tokens)}"
#            print(ln); bin_lines.append(ln)
#
#        if not suppress_end:
#            print("[dumpcmp] end")
#
#        entry={
#            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
#            "a": a,
#            "b": b,
#            "changed_rows": changed_rows,
#            "changed_bytes": changed_bytes,
#            "hex_lines": hex_lines,
#            "binary_lines": bin_lines
#        }
#        _store_cmp_result(entry)
#        return {"a":a,"b":b,"changed_rows":changed_rows,"changed_bytes":changed_bytes}
#
#    def dump_compare(a,b):
#        return _dump_compare_single(a,b)
#
#    def parse_multi_pairs(arg_str):
#        parts=[p.strip() for p in arg_str.split(",") if p.strip()]
#        pairs=[]
#        for p in parts:
#            toks=p.split()
#            if len(toks)!=2:
#                print(f"[dumpcmp] skip invalid pair '{p}'")
#                continue
#            da,db=toks
#            if da in DIGIT_SLOTS and db in DIGIT_SLOTS:
#                pairs.append((da,db))
#            else:
#                print(f"[dumpcmp] skip non-digit pair '{p}'")
#        return pairs
#
#    def multi_dump_compare(pairs):
#        if not pairs:
#            print("[dumpcmp] no valid pairs"); return
#        session_stats=[]
#        print(f"[dumpcmp] multi compare {len(pairs)} pair(s): {', '.join(f'{a}-{b}' for a,b in pairs)}")
#        for idx,(a,b) in enumerate(pairs,1):
#            print(f"\n[dumpcmp] pair {idx}/{len(pairs)} ({a} vs {b})")
#            stats=_dump_compare_single(a,b,suppress_end=True)
#            if stats:
#                print("[dumpcmp] end")
#                session_stats.append(stats)
#        entry={
#            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
#            "pairs":[{"a":s["a"],"b":s["b"],"changed_rows":s["changed_rows"],"changed_bytes":s["changed_bytes"]} for s in session_stats]
#        }
#        cmp_history.append(entry)
#        save_cmp_history(DUMPCMP_HISTORY_FILE,cmp_history)
#        total_changed=sum(s["changed_bytes"] for s in session_stats)
#        print(f"\n[dumpcmp] multi summary: {len(session_stats)} compared, total changed bytes={total_changed}")
#        for s in session_stats:
#            print(f"  {s['a']} vs {s['b']}: rows={s['changed_rows']} bytes={s['changed_bytes']}")
#
#    def show_cmp_history(limit=10):
#        print("[cmphist] recent multi-compare sessions:")
#        tail=cmp_history[-limit:]
#        if not tail:
#            print(" (none)"); return
#        for idx,entry in enumerate(tail,1):
#            ts=entry.get("timestamp","?")
#            pairs_txt=", ".join(f"{p['a']}-{p['b']}:{p['changed_bytes']}" for p in entry.get("pairs",[]))
#            print(f" {idx}. {ts}  {pairs_txt}")
#
#    def show_cmp_results(limit=10):
#        print("[cmpres] stored compare entries (latest first):")
#        tail=_dumpcmp_results[-limit:]
#        if not tail:
#            print(" (none)")
#            return
#        for i,entry in enumerate(tail,1):
#            print(f" {i}. {entry.get('timestamp','?')} {entry.get('a')}-{entry.get('b')} "
#                  f"bytes={entry.get('changed_bytes')} rows={entry.get('changed_rows')} "
#                  f"hex_lines={len(entry.get('hex_lines',[]))} bin_lines={len(entry.get('binary_lines',[]))}")
#
#    def clear_cmp_results():
#        global _dumpcmp_results
#        _dumpcmp_results=[]
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#        print("[cmpres] all stored pair compare results cleared.")
#
#    def show_all():
#        show_slots(); show_global_combos(); dumplist()
#
#    def print_help():
#        print("""[help]
#(all commands are lowercase)
#slots (0-9,a-z):
#  /setX <text>   /comboX <seq>  /enterX  /clrX  oX  /slots  /slotsave  /slotload
#global combos (0-9):
#  /cset d <seq>  /clist  /crun d  /cclear d  /crun_all  /csave  /cload
#i2cdump capture:
#  /dumpsave d    /dumpshow d    /dumplist
#  /dumpcmp a b
#  /dumpcmp a b,c d,e f   (multi compare pairs, alias: /dumpcmpmulti)
#  /cmphist [n]    show recent multi-compare history
#  /cmpres  [n]    list stored compare result entries
#  /cmpresclear    clear ALL stored detailed pair results (confirmation)
#delays & modes:
#  /delay /scriptdelay /linedelay /hex on|off /scriptecho on|off
#general:
#  /help /quit
#hotkeys (win):
#  Ctrl+0..9 / Ctrl+a..z play slot
#  Ctrl+S show slots+combos+dumps
#  C+B+digit run digit combo
#  C+L list digit combos
#""")
#
#    def play_slot_recursive(idx_char, depth, visited, script_ctx):
#        if depth>40:
#            print("[play] depth limit"); return
#        if idx_char not in slot_cmds:
#            print(f"[play] slot {idx_char} not found"); return
#        v=slot_cmds[idx_char]
#        if v is None:
#            print(f"[play] slot {idx_char} empty"); return
#        if id(v) in visited:
#            print(f"[play] cycle at {idx_char}"); return
#        visited.add(id(v))
#        t=v.get("type")
#        if t=="enter":
#            send_enter_only(safe=True, script_ctx=script_ctx)
#        elif t=="combo":
#            for c in v.get("seq",""):
#                if c in slot_cmds:
#                    play_slot_recursive(c, depth+1, visited, script_ctx)
#        else:
#            data=v.get("data","")
#            parts=data.split(TOKEN_ENTER)
#            for pi,segment in enumerate(parts):
#                lines=segment.splitlines()
#                if not lines and segment=="":
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#                for line in lines:
#                    if line.strip()=="" and line!="":
#                        send_enter_only(safe=True, script_ctx=script_ctx)
#                    elif line!="":
#                        send_line(line,safe=True,
#                                  local_echo=f"[run] {line}" if SCRIPT_LOCAL_ECHO else None,
#                                  script_ctx=script_ctx)
#                if pi<len(parts)-1:
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#        visited.remove(id(v))
#
#    def play_slot(k):
#        if k not in slot_cmds:
#            print(f"[play] slot {k} invalid"); return
#        print(f"[play] slot {k}")
#        ctx=ScriptContext()
#        play_slot_recursive(k,0,set(),ctx)
#
#    def run_global_combo(d):
#        if d not in global_combos:
#            print(f"[combo] digit {d} undefined"); return
#        seq=global_combos[d]; print(f"[combo] run {d}: {seq}")
#        ctx=ScriptContext()
#        for c in seq:
#            if c in slot_cmds:
#                play_slot_recursive(c,0,set(),ctx)
#
#    def run_all_global_combos():
#        defined=[d for d in DIGIT_SLOTS if d in global_combos]
#        if not defined:
#            print("[combo] no digit combos defined"); return
#        print("[combo] run all digit combos:")
#        ctx=ScriptContext()
#        for d in defined:
#            seq=global_combos[d]; print(f"  -> {d}: {seq}")
#            for c in seq:
#                if c in slot_cmds:
#                    play_slot_recursive(c,0,set(),ctx)
#
#    def run_single_combo_via_hotkey(d):
#        if d in global_combos:
#            print(f"[combo] (hotkey) {d}")
#            run_global_combo(d)
#        else:
#            print(f"[combo] (hotkey) {d} undefined")
#
#    stop_hotkey=threading.Event()
#    hotkey_thread=None
#    if os.name=='nt':
#        try:
#            hotkey_thread=HotkeyThread(
#                play_callback=play_slot,
#                show_all_callback=show_all,
#                combo_list_callback=show_global_combos,
#                run_single_combo_callback=run_single_combo_via_hotkey,
#                stop_event=stop_hotkey
#            )
#            hotkey_thread.start()
#        except Exception as e:
#            print(f"[WARN] hotkey thread failed: {e}")
#
#    # Command loop
#    try:
#        while True:
#            try:
#                line=input()
#            except EOFError:
#                break
#            stripped=line.strip()
#            lower=stripped.lower()
#
#            if lower=="/help":
#                print_help(); continue
#
#            # i2c dump commands
#            if lower.startswith("/dumpsave"):
#                parts=lower.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[dumps] usage: /dumpsave <digit>")
#                else:
#                    d=parts[1]
#                    if _last_captured_dump:
#                        i2c_dump_slots[d]=_last_captured_dump[:]
#                        print(f"[dumps] saved capture to slot {d} ({len(_last_captured_dump)} lines)")
#                        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#                    else:
#                        print("[dumps] no captured dump to save")
#                continue
#
#            if lower.startswith("/dumpshow"):
#                parts=lower.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[dumps] usage: /dumpshow <digit>")
#                else:
#                    dump_show(parts[1])
#                continue
#
#            if lower=="/dumplist":
#                dumplist(); continue
#
#            # dumpcmp (single/multi)
#            if lower.startswith("/dumpcmp") or lower.startswith("/dumpcmpmulti"):
#                cmd,*rest=lower.split(None,1)
#                if not rest:
#                    print("[dumpcmp] usage: /dumpcmp a b  OR /dumpcmp a b,c d")
#                    continue
#                rem=rest[0].strip()
#                if ',' in rem or len(rem.split())>2:
#                    if ',' not in rem:
#                        toks=rem.split()
#                        if len(toks)>=4 and len(toks)%2==0:
#                            pair_strs=[f"{toks[i]} {toks[i+1]}" for i in range(0,len(toks),2)]
#                            rem=",".join(pair_strs)
#                    pairs=parse_multi_pairs(rem)
#                    multi_dump_compare(pairs)
#                else:
#                    parts=rem.split()
#                    if len(parts)!=2 or parts[0] not in DIGIT_SLOTS or parts[1] not in DIGIT_SLOTS:
#                        print("[dumpcmp] usage: /dumpcmp <a> <b>")
#                    else:
#                        dump_compare(parts[0],parts[1])
#                continue
#
#            if lower.startswith("/cmphist"):
#                parts=lower.split()
#                limit=10
#                if len(parts)==2 and parts[1].isdigit():
#                    limit=max(1,min(100,int(parts[1])))
#                show_cmp_history(limit)
#                continue
#
#            if lower.startswith("/cmpresclear"):
#                confirm=input("type yes to confirm clearing all stored compare results: ").strip().lower()
#                if confirm=="yes":
#                    clear_cmp_results()
#                else:
#                    print("[cmpres] cancelled.")
#                continue
#
#            if lower.startswith("/cmpres"):
#                parts=lower.split()
#                limit=10
#                if len(parts)==2 and parts[1].isdigit():
#                    limit=max(1,min(200,int(parts[1])))
#                show_cmp_results(limit)
#                continue
#
#            # Delays & modes
#            if lower.startswith("/delay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[delay] {char_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        char_delay=v; print(f"[delay] -> {char_delay} ms"); persist_user()
#                    except: print(f"[delay] invalid: {parts[1]}")
#                continue
#
#            if lower.startswith("/scriptdelay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptdelay] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[scriptdelay] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
#                    except: print(f"[scriptdelay] invalid: {parts[1]}")
#                continue
#
#            if lower.startswith("/linedelay"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[linedelay] {line_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        line_delay=v; print(f"[linedelay] -> {line_delay} ms"); persist_user()
#                    except: print(f"[linedelay] invalid: {parts[1]}")
#                continue
#
#            if lower.startswith("/scriptecho"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[scriptecho] {'on' if SCRIPT_LOCAL_ECHO else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        SCRIPT_LOCAL_ECHO=(arg=="on")
#                        print(f"[scriptecho] -> {'on' if SCRIPT_LOCAL_ECHO else 'off'}"); persist_user()
#                    else:
#                        print("[scriptecho] use: /scriptecho on|off")
#                continue
#
#            if lower.startswith("/hex"):
#                parts=lower.split(None,1)
#                if len(parts)==1:
#                    print(f"[hex] {'on' if TX_HEX else 'off'}")
#                else:
#                    arg=parts[1]
#                    if arg in ("on","off"):
#                        TX_HEX=(arg=="on")
#                        print(f"[hex] -> {'on' if TX_HEX else 'off'}"); persist_user()
#                    else:
#                        print("[hex] use: /hex on|off")
#                continue
#
#            # Slots persistence
#            if lower=="/slotsave":
#                save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
#            if lower=="/slotload":
#                load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue
#
#            # Combos
#            if lower=="/clist":
#                show_global_combos(); continue
#            if lower.startswith("/cset "):
#                parts=stripped.split(None,2)  # use original to keep data
#                # but interpret in lowercase
#                parts_l=lower.split(None,2)
#                if len(parts_l)<3:
#                    print("[combo] usage: /cset <digit> <seq>")
#                else:
#                    digit=parts_l[1]
#                    if not (digit.isdigit() and len(digit)==1):
#                        print("[combo] name must be single digit (0-9)")
#                    else:
#                        seq="".join(ch for ch in parts_l[2] if ch.isalnum())
#                        global_combos[digit]=seq
#                        print(f"[combo] {digit} = {seq}")
#                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                continue
#            if lower.startswith("/crun "):
#                digit=lower.split(None,1)[1].strip()
#                run_global_combo(digit); continue
#            if lower.startswith("/cclear "):
#                digit=lower.split(None,1)[1].strip()
#                if digit in global_combos:
#                    del global_combos[digit]; print(f"[combo] cleared {digit}")
#                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                else:
#                    print(f"[combo] {digit} not defined")
#                continue
#            if lower=="/csave":
#                save_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if lower=="/cload":
#                load_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if lower=="/crun_all":
#                run_all_global_combos(); continue
#
#            # General
#            if lower=="/quit":
#                print("[info] quit")
#                break
#            if lower=="/slots":
#                show_slots(); continue
#
#            # Slot definitions (/enterx /combox /setx /clrx)
#            if lower.startswith("/enter") and len(lower)==7:
#                key=lower[6]
#                if key in slot_cmds:
#                    slot_cmds[key]={"type":"enter"}
#                    print(f"[set] slot {key} = <enter>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/combo") and len(lower)>=7:
#                key=lower[6]
#                if key in slot_cmds:
#                    parts=stripped.split(None,1)
#                    seq=""
#                    if len(parts)>1:
#                        seq="".join(ch for ch in parts[1] if ch.isalnum())
#                    slot_cmds[key]={"type":"combo","seq":seq}
#                    print(f"[set] slot {key} = <combo {seq}>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/set") and len(lower)>=5:
#                key=lower[4]
#                if key in slot_cmds:
#                    parts=stripped.split(None,1)
#                    data=parts[1] if len(parts)>1 else ""
#                    slot_cmds[key]={"type":"raw","data":data}
#                    print(f"[set] slot {key} raw length={len(data)}")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if lower.startswith("/clr") and len(lower)==5:
#                key=lower[4]
#                if key in slot_cmds:
#                    slot_cmds[key]=None
#                    print(f"[clr] slot {key} cleared")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            # Play slot (ox)
#            if len(lower)==2 and lower[0]=='o':
#                key=lower[1]
#                if key in slot_cmds:
#                    play_slot(key)
#                continue
#
#            # Blank line -> ENTER
#            if line=="":
#                send_enter_only(safe=False)
#                continue
#
#            # Normal input (user data may have case; we use original line)
#            try:
#                body=line.encode(ENCODING,errors="replace")
#            except Exception as e:
#                print(f"[WARN] encode failed: {e}")
#                continue
#            send_bytes(body+line_suffix(), safe=False, tag="TX")
#
#    except KeyboardInterrupt:
#        print("\n[INFO] keyboardinterrupt")
#    finally:
#        persist_user()
#        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#        save_cmp_history(DUMPCMP_HISTORY_FILE, cmp_history)
#        save_cmp_results(DUMPCMP_RESULTS_FILE)
#        if 'hotkey_thread' in locals() and hotkey_thread:
#            stop_hotkey.set()
#            hotkey_thread.join(timeout=0.5)
#        reader.stop()
#        time.sleep(0.05)
#        try: ser.close()
#        except: pass
#        if 'log_file' in locals() and log_file:
#            try: log_file.close()
#            except: pass
#        print("[INFO] exit")
#
#if __name__ == "__main__":
#    main()
#    
#
#
#
#
#
#
#
#    """

































    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
250904_0001_i2cdump_data_compare_pass
250904_0002_i2cdump_data_compare_binary_pass
250904_0003_i2cdump_data_multiple_compare_pass

    """




#    """
##!/usr/bin/env python
## -*- coding: utf-8 -*-
#"""
#Serial console with:
# - Slots 0-9 + a-z (36 total)
# - Digit global combos (0-9)
# - Hotkeys: Ctrl+0..9 / Ctrl+a..z (play slot), Ctrl+S (show all), C+B+<digit> (single combo), C+L (list combos)
# - i2cdump capture & storage (/dumpsave /dumpshow /dumplist /dumpcmp)
# - Tolerant i2cdump capture (header or first data row, prompt line, overflow guard)
# - /dumpcmp:
#     hex
#       disk:<a> (unchanged => XX, changed => HEX from A)
#       disk:<b> (unchanged => XX, changed => HEX from B)
#     binary
#       disk:<a> row lines (unchanged => XX, changed => 8-bit binary xxxx_xxxx)
#       disk:<b> row lines (same rule)
#     (Binary section prints rows once per dump with changed bytes shown inline.)
# - NEW: Multi-compare support:
#     /dumpcmp 1 2,2 3,3 4
#       -> compares (1,2) then (2,3) then (3,4) in sequence (any digit pairs)
#     /dumpcmpmulti 1 2,2 3,3 4   (alias)
#     Results are printed sequentially and a summary record is appended to
#     .dumpcmp_history.json with per-pair statistics.
#     Single pair usage (/dumpcmp a b) unchanged.
#
#Receiver thread style preserved (only feed hook).
#"""
#
#import sys
#import serial
#import threading
#import time
#import os
#import json
#import re
#from datetime import datetime
#
#try:
#    from serial.tools import list_ports
#except ImportError:
#    list_ports = None
#
## ================== Config (overridden by saved user config) ==================
#PORT                    = "COM5"
#BAUD                    = 115200
#PARITY_NAME             = "none"
#DATA_BITS               = 8
#STOP_BITS               = 1
#FLOW_CTRL               = "none"
#ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
#ENCODING                = "utf-8"
#TIMEOUT                 = 0.05
#CHAR_DELAY_MS           = 0
#LINE_DELAY_MS           = 0
#ASSERT_DTR              = False
#ASSERT_RTS              = False
#CLEAR_BUFF_ON_OPEN      = False
#
#TX_HEX                  = True
#HEX_DUMP_RX             = False
#RAW_RX                  = False
#QUIET_RX                = False
#
#LOG_PATH                = None
#INI_PATH                = None
#NO_BANNER               = False
#
#INTERACTIVE_SELECT      = True
#REMEMBER_LAST           = True
#LAST_FILE_NAME          = ".last_port"
#
#SLOTS_SAVE_FILE         = ".slot_cmds.json"
#AUTO_SAVE_SLOTS         = True
#SHOW_SAVE_MESSAGE       = True
#
#COMBO_SAVE_FILE         = ".combo_defs.json"
#AUTO_SAVE_COMBOS        = True
#SHOW_COMBO_SAVE_MSG     = True
#
#USER_CONFIG_FILE        = ".console_config.json"
#AUTO_SAVE_CONFIG        = True
#
#I2C_DUMP_SAVE_FILE      = ".i2c_dumps.json"
#AUTO_SAVE_I2C_DUMPS     = True
#MAX_I2C_DUMPS           = 10   # 0-9
#
#SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
#SCRIPT_LOCAL_ECHO         = False
#
#PROMPT_PATTERN            = "i2c>"
#SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
#SCRIPT_WAIT_PROMPT        = True
#POST_PROMPT_STABILIZE_MS  = 5
#
#HOTKEY_POLL_INTERVAL_SEC  = 0.05
#TOKEN_ENTER               = "<ENTER>"
#
#DIGIT_SLOTS  = [str(i) for i in range(10)]
#LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
#ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS
#
#DUMPCMP_HISTORY_FILE     = ".dumpcmp_history.json"
#MAX_CMP_HISTORY_ENTRIES  = 200
#
## ======================================================================
## Utility
## ======================================================================
#def format_hex(data: bytes) -> str:
#    return " ".join(f"{b:02X}" for b in data)
#
#def parse_ini(path: str):
#    out = {}
#    if not path or not os.path.isfile(path):
#        return out
#    try:
#        with open(path, "r", encoding="utf-8", errors="ignore") as f:
#            for line in f:
#                line=line.strip()
#                if not line or line.startswith(";") or "=" not in line:
#                    continue
#                k,v=line.split("=",1)
#                k=k.strip(); v=v.strip()
#                kl=k.lower()
#                if kl in ("comport","baudrate","delayperchar","delayperline"):
#                    try: out[k]=int(v)
#                    except: pass
#                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
#                    out[k]=v
#    except Exception as e:
#        print(f"[WARN] INI parse failed: {e}")
#    return out
#
#def load_user_config():
#    if not os.path.isfile(USER_CONFIG_FILE):
#        return {}
#    try:
#        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        return data if isinstance(data,dict) else {}
#    except Exception as e:
#        print(f"[CFG] Load failed: {e}")
#        return {}
#
#def save_user_config(cfg):
#    if not AUTO_SAVE_CONFIG: return
#    try:
#        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
#            json.dump(cfg,f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[CFG] Save failed: {e}")
#
#def normalize_slot_value(v):
#    if v is None: return None
#    if isinstance(v,dict):
#        t=v.get("type")
#        if t=="raw":
#            d=v.get("data"); return {"type":"raw","data": d if isinstance(d,str) else ""}
#        if t=="enter": return {"type":"enter"}
#        if t=="combo":
#            seq=v.get("seq","")
#            if not isinstance(seq,str): seq=""
#            return {"type":"combo","seq":seq}
#        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
#    if isinstance(v,str): return {"type":"raw","data":v}
#    return {"type":"raw","data":str(v)}
#
#def load_slots_from_file(path, slot_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        changed=False
#        for k in slot_dict.keys():
#            if k in data:
#                slot_dict[k]=normalize_slot_value(data[k]); changed=True
#        if changed: print(f"[SLOTS] Loaded {path}")
#    except Exception as e:
#        print(f"[SLOTS] Load failed: {e}")
#
#def save_slots_to_file(path, slot_dict):
#    try:
#        out={k:(None if v is None else v) for k,v in slot_dict.items()}
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(out,f,ensure_ascii=False,indent=2)
#        if SHOW_SAVE_MESSAGE: print(f"[SLOTS] Saved -> {path}")
#    except Exception as e:
#        print(f"[SLOTS] Save failed: {e}")
#
#def load_global_combos(path, combo_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
#                    combo_dict[k]="".join(ch for ch in v if ch.isalnum())
#            print(f"[COMBO] Loaded {path} ({len(combo_dict)} items)")
#    except Exception as e:
#        print(f"[COMBO] Load failed: {e}")
#
#def save_global_combos(path, combo_dict):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
#        if SHOW_COMBO_SAVE_MSG: print(f"[COMBO] Saved -> {path}")
#    except Exception as e:
#        print(f"[COMBO] Save failed: {e}")
#
#def load_i2c_dumps(path, dump_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if k in dump_dict and isinstance(v,list):
#                    dump_dict[k]=v
#        print(f"[DUMPS] Loaded {path}")
#    except Exception as e:
#        print(f"[DUMPS] Load failed: {e}")
#
#def save_i2c_dumps(path, dump_dict):
#    if not AUTO_SAVE_I2C_DUMPS: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(dump_dict,f,ensure_ascii=False,indent=2)
#        print(f"[DUMPS] Saved -> {path}")
#    except Exception as e:
#        print(f"[DUMPS] Save failed: {e}")
#
#def load_cmp_history(path):
#    if not os.path.isfile(path): return []
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,list): return data
#    except Exception as e:
#        print(f"[CMPHIST] Load failed: {e}")
#    return []
#
#def save_cmp_history(path, hist):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(hist[-MAX_CMP_HISTORY_ENTRIES:],f,ensure_ascii=False,indent=2)
#        print(f"[CMPHIST] Saved -> {path}")
#    except Exception as e:
#        print(f"[CMPHIST] Save failed: {e}")
#
## ======================================================================
## Prompt tracking
## ======================================================================
#prompt_lock=threading.Lock()
#prompt_seq=0
#def inc_prompt_if_in(text:str):
#    global prompt_seq
#    if PROMPT_PATTERN and PROMPT_PATTERN in text:
#        with prompt_lock:
#            prompt_seq+=1
#def get_prompt_seq():
#    with prompt_lock:
#        return prompt_seq
#def wait_for_next_prompt(prev_seq, timeout):
#    if not SCRIPT_WAIT_PROMPT: return prev_seq
#    deadline=time.time()+timeout
#    while time.time()<deadline:
#        cur=get_prompt_seq()
#        if cur>prev_seq:
#            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
#            return cur
#        time.sleep(0.01)
#    return get_prompt_seq()
#
## ======================================================================
## i2cdump capture logic
## ======================================================================
#_i2c_capture_buffer_fragment=""
#_i2c_capture_active=False
#_i2c_capture_lines=[]
#_last_captured_dump=None
#
#_I2C_HEADER_RE = re.compile(r'^\s+00(?:\s+[0-9A-Fa-f]{2}){15}\s*$')
#_I2C_DATA_ROW_RE = re.compile(r'^[0-9A-Fa-f]{2}:\s+([0-9A-Fa-f]{2}\s+){0,15}[0-9A-Fa-f]{2}\s*$')
#_LAST_ADDR = "f0"
#
#def _maybe_finalize_partial(reason:str):
#    global _i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if _i2c_capture_active and _i2c_capture_lines:
#        _last_captured_dump=_i2c_capture_lines[:]
#        print(f"\n[DUMPS] Captured ({reason}) {len(_last_captured_dump)} lines")
#    _i2c_capture_active=False
#    _i2c_capture_lines=[]
#
#def _i2c_capture_feed(chunk:str):
#    global _i2c_capture_buffer_fragment,_i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if not chunk: return
#    _i2c_capture_buffer_fragment += chunk
#    while True:
#        if '\n' not in _i2c_capture_buffer_fragment:
#            break
#        line,rest=_i2c_capture_buffer_fragment.split('\n',1)
#        _i2c_capture_buffer_fragment=rest
#        line=line.rstrip('\r')
#        if PROMPT_PATTERN and line.startswith(PROMPT_PATTERN):
#            if _i2c_capture_active:
#                _maybe_finalize_partial("prompt")
#            continue
#        if not _i2c_capture_active:
#            if _I2C_HEADER_RE.match(line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=[line]
#                continue
#            if re.match(r'^00:\s', line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=["#NO_HEADER#"]
#            else:
#                continue
#        if _i2c_capture_active:
#            if _I2C_DATA_ROW_RE.match(line) or line==_i2c_capture_lines[0]:
#                if line != _i2c_capture_lines[0]:
#                    _i2c_capture_lines.append(line)
#            else:
#                if line.strip():
#                    _i2c_capture_lines.append(line)
#            if line.lower().startswith(_LAST_ADDR + ":"):
#                _last_captured_dump=_i2c_capture_lines[:]
#                print(f"\n[DUMPS] Captured i2cdump ({len(_last_captured_dump)} lines)")
#                _i2c_capture_active=False
#                _i2c_capture_lines=[]
#                continue
#            if len(_i2c_capture_lines) > 60:
#                _maybe_finalize_partial("overflow")
#                continue
#
## ======================================================================
## Receiver thread
## ======================================================================
#class SerialReaderThread(threading.Thread):
#    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
#        super().__init__(daemon=True)
#        self.ser=ser; self.encoding=encoding
#        self.hex_dump=hex_dump; self.raw=raw
#        self.log_file=log_file; self.quiet=quiet
#        self._running=True
#    def stop(self): self._running=False
#    def run(self):
#        while self._running and self.ser.is_open:
#            try:
#                data=self.ser.read(self.ser.in_waiting or 1)
#            except serial.SerialException as e:
#                print(f"[ERR] Serial exception: {e}")
#                break
#            if not data: continue
#            if self.log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try:
#                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n"); self.log_file.flush()
#                except Exception: pass
#            if self.quiet: continue
#            if self.hex_dump:
#                txt=format_hex(data)
#                print(f"[RX HEX] {txt}")
#                inc_prompt_if_in(txt)
#                _i2c_capture_feed(txt+"\n")
#            elif self.raw:
#                sys.stdout.buffer.write(data); sys.stdout.flush()
#                try:
#                    decoded=data.decode(self.encoding,errors="ignore")
#                    inc_prompt_if_in(decoded)
#                    _i2c_capture_feed(decoded)
#                except: pass
#            else:
#                try:
#                    text=data.decode(self.encoding,errors="replace")
#                except Exception:
#                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
#                print(text,end="",flush=True)
#                inc_prompt_if_in(text)
#                _i2c_capture_feed(text)
#
## ======================================================================
## Port selection
## ======================================================================
#def load_last_port():
#    if not REMEMBER_LAST: return None
#    try:
#        if os.path.isfile(LAST_FILE_NAME):
#            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
#                v=f.read().strip()
#                if v: return v
#    except: pass
#    return None
#def save_last_port(p):
#    if not REMEMBER_LAST: return
#    try:
#        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
#            f.write(p.strip())
#    except: pass
#
#def interactive_select_port(default_port):
#    port=default_port; baud=BAUD; parity_name=PARITY_NAME
#    data_bits=DATA_BITS; stop_bits=STOP_BITS; flow_ctrl=FLOW_CTRL; enter_mode=ENTER_MODE
#    last=load_last_port()
#    if last: default_port=last
#    if not INTERACTIVE_SELECT:
#        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#    print("=== Serial Interactive Config (Enter to keep default) ===")
#    if list_ports:
#        ports=list(list_ports.comports())
#        if ports:
#            print("Available ports:")
#            for idx,p in enumerate(ports,1):
#                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
#        else:
#            print("No COM ports detected.")
#    val=input(f"Port [{default_port}]: ").strip()
#    if val: port=val
#    val=input(f"Baud [{baud}]: ").strip()
#    if val.isdigit(): baud=int(val)
#    plist=["none","even","odd","mark","space"]
#    val=input(f"Parity {plist} [{parity_name}]: ").strip().lower()
#    if val in plist: parity_name=val
#    val=input(f"Data bits (7/8) [{data_bits}]: ").strip()
#    if val in ("7","8"): data_bits=int(val)
#    val=input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
#    if val in ("1","2"): stop_bits=int(val)
#    flist=["none","rtscts","dsrdtr","x"]
#    val=input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
#    if val in flist: flow_ctrl=val
#    emlist=["CR","CRLF","LF","NONE"]
#    val=input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
#    if val in emlist: enter_mode=val
#    save_last_port(port)
#    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#
## ======================================================================
## Hotkey Thread
## ======================================================================
#class HotkeyThread(threading.Thread):
#    def __init__(self,
#                 play_callback,
#                 show_all_callback,
#                 combo_list_callback,
#                 run_single_combo_callback,
#                 stop_event):
#        super().__init__(daemon=True)
#        self.play_callback=play_callback
#        self.show_all_callback=show_all_callback
#        self.combo_list_callback=combo_list_callback
#        self.run_single_combo_callback=run_single_combo_callback
#        self.stop_event=stop_event
#        import ctypes
#        self.ctypes=ctypes
#        self.user32=ctypes.WinDLL("user32", use_last_error=True)
#        self.VK_CTRL=0x11; self.VK_S=0x53
#        self.VK_C=0x43; self.VK_B=0x42; self.VK_L=0x4C
#        self.VK_0_9=list(range(0x30,0x3A))
#        self.VK_NUM_0_9=list(range(0x60,0x6A))
#        self.VK_A_Z=list(range(0x41,0x5B))
#        self.prev_digit_down={vk:False for vk in self.VK_0_9+self.VK_NUM_0_9}
#        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
#        self.prev_s_down=False
#        self.prev_cb=False
#        self.prev_cl_combo_list=False
#    def key_down(self,vk):
#        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
#    def run(self):
#        while not self.stop_event.is_set():
#            ctrl=self.key_down(self.VK_CTRL)
#            s_now=ctrl and self.key_down(self.VK_S)
#            if s_now and not self.prev_s_down:
#                print(); self.show_all_callback()
#            self.prev_s_down=s_now
#            c_now=self.key_down(self.VK_C); b_now=self.key_down(self.VK_B)
#            cb_now=c_now and b_now
#            if cb_now:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        self.run_single_combo_callback(key)
#                    self.prev_digit_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#            self.prev_cb=cb_now
#            l_now=self.key_down(self.VK_L); cl_now=c_now and l_now
#            if cl_now and not self.prev_cl_combo_list:
#                print(); self.combo_list_callback()
#            self.prev_cl_combo_list=cl_now
#            if ctrl:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        print(); self.play_callback(key.lower())
#                    self.prev_digit_down[vk]=now
#                for vk in self.VK_A_Z:
#                    now=self.key_down(vk)
#                    if now and not self.prev_letter_down[vk]:
#                        key=chr(vk).lower()
#                        print(); self.play_callback(key)
#                    self.prev_letter_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
#                self.prev_s_down=False
#            time.sleep(HOTKEY_POLL_INTERVAL_SEC)
#
## ======================================================================
## Main
## ======================================================================
#def main():
#    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO, _last_captured_dump
#    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
#    user_cfg=load_user_config()
#
#    if "char_delay_ms" in user_cfg:
#        try: globals()['CHAR_DELAY_MS']=float(user_cfg["char_delay_ms"])
#        except: pass
#    if "line_delay_ms" in user_cfg:
#        try: globals()['LINE_DELAY_MS']=float(user_cfg["line_delay_ms"])
#        except: pass
#    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
#    if "script_char_delay_ms" in user_cfg:
#        try:
#            v=float(user_cfg["script_char_delay_ms"])
#            if v>=0: SAFE_SCRIPT_CHAR_DELAY_MS=v
#        except: pass
#    if "script_local_echo" in user_cfg:
#        SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
#
#    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
#    init_baud=cfg_ini.get("BaudRate",BAUD)
#    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
#    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
#    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
#    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
#    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
#    if init_enter not in ("CR","CRLF","LF","NONE"): init_enter="CR"
#
#    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)
#
#    parity_map={"even":serial.PARITY_EVEN,"odd":serial.PARITY_ODD,"none":serial.PARITY_NONE,
#                "mark":serial.PARITY_MARK,"space":serial.PARITY_SPACE}
#    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
#    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
#    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE
#
#    if fc in ("rtscts","hard"):
#        rtscts,dsrdtr,xonxoff=True,False,False
#    elif fc=="dsrdtr":
#        rtscts,dsrdtr,xonxoff=False,True,False
#    elif fc=="x":
#        rtscts,dsrdtr,xonxoff=False,False,True
#    else:
#        rtscts=dsrdtr=xonxoff=False
#
#    char_delay=float(globals()['CHAR_DELAY_MS']); line_delay=float(globals()['LINE_DELAY_MS'])
#
#    try:
#        ser=serial.Serial(port,baud,timeout=TIMEOUT,
#                          bytesize=bytesize,parity=parity,stopbits=stopbits,
#                          rtscts=rtscts,dsrdtr=dsrdtr,xonxoff=xonxoff,write_timeout=1)
#    except serial.SerialException as e:
#        print(f"[ERR] Cannot open {port}: {e}"); return
#
#    try:
#        if ASSERT_DTR: ser.setDTR(True)
#        if ASSERT_RTS: ser.setRTS(True)
#    except Exception as e:
#        print(f"[WARN] Setting DTR/RTS failed: {e}")
#
#    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
#        try: ser.reset_input_buffer(); ser.reset_output_buffer()
#        except Exception as e: print(f"[WARN] Clear buffers failed: {e}")
#
#    if not NO_BANNER:
#        print(f"[INFO] Opened {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
#        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
#        print(f"[INFO] Enter={enter_mode} char_delay={char_delay}ms line_delay={line_delay}ms script_min={SAFE_SCRIPT_CHAR_DELAY_MS}ms hex={'ON' if TX_HEX else 'OFF'} echo={'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#        print("[INFO] Type /help for command list.")
#
#    log_file=None
#    if LOG_PATH:
#        try:
#            log_file=open(LOG_PATH,"a",encoding="utf-8")
#            print(f"[INFO] Logging to {LOG_PATH}")
#        except Exception as e:
#            print(f"[WARN] Log open failed: {e}")
#
#    reader=SerialReaderThread(
#        ser,encoding=ENCODING,hex_dump=HEX_DUMP_RX,raw=RAW_RX,
#        log_file=log_file,quiet=QUIET_RX
#    )
#    reader.start()
#
#    send_lock=threading.Lock()
#
#    def persist_user():
#        user_cfg["char_delay_ms"]=char_delay
#        user_cfg["line_delay_ms"]=line_delay
#        user_cfg["tx_hex"]=TX_HEX
#        user_cfg["script_char_delay_ms"]=SAFE_SCRIPT_CHAR_DELAY_MS
#        user_cfg["script_local_echo"]=SCRIPT_LOCAL_ECHO
#        save_user_config(user_cfg)
#
#    def line_suffix():
#        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]
#
#    def send_bytes(data:bytes, tag="TX", safe=False, local_echo_line=None):
#        if not data: return
#        per_char_delay=char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
#        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
#            print(local_echo_line)
#        if per_char_delay>0 and len(data)>1:
#            for i,b in enumerate(data):
#                with send_lock:
#                    try: ser.write(bytes([b])); ser.flush()
#                    except serial.SerialException as e:
#                        print(f"[ERR] TX failed: {e}"); return
#                if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(bytes([b]))}")
#                if log_file:
#                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
#                    except: pass
#                if i<len(data)-1: time.sleep(per_char_delay/1000.0)
#        else:
#            with send_lock:
#                try: ser.write(data); ser.flush()
#                except serial.SerialException as e:
#                    print(f"[ERR] TX failed: {e}"); return
#            if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(data)}")
#            if log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
#                except: pass
#        if line_delay>0 and tag.startswith("TX"): time.sleep(line_delay/1000.0)
#
#    class ScriptContext:
#        def __init__(self):
#            self.last_prompt_seq=get_prompt_seq(); self.first_send=True
#        def wait_ready_if_needed(self):
#            if not SCRIPT_WAIT_PROMPT: return
#            if self.first_send:
#                self.first_send=False; return
#            prev=self.last_prompt_seq
#            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
#        def note_after_send(self): pass
#
#    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        try: body=text.encode(ENCODING,errors="replace")
#        except Exception as e: print(f"[WARN] Encode failed: {e}"); return
#        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    def send_enter_only(safe=False, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        send_bytes(line_suffix(), tag="TX-EMPTY", safe=safe)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    slot_cmds={k:None for k in ALL_SLOTS}; load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
#    global_combos={}; load_global_combos(COMBO_SAVE_FILE, global_combos)
#    i2c_dump_slots={str(i):None for i in range(MAX_I2C_DUMPS)}; load_i2c_dumps(I2C_DUMP_SAVE_FILE, i2c_dump_slots)
#    cmp_history=load_cmp_history(DUMPCMP_HISTORY_FILE)
#
#    def show_slots():
#        print("[SLOTS] ---------------------------")
#        for k in DIGIT_SLOTS+LETTER_SLOTS:
#            v=slot_cmds.get(k)
#            if v is None:
#                print(f" {k}: (empty)")
#            else:
#                t=v.get("type")
#                if t=="enter":
#                    print(f" {k}: <ENTER>")
#                elif t=="combo":
#                    print(f" {k}: <COMBO {v.get('seq','')}>")
#                else:
#                    data=v.get("data","")
#                    first=data.splitlines()[0] if data else ""
#                    more=" ..." if "\n" in data else ""
#                    print(f" {k}: {first[:60]}{more}")
#        print("[SLOTS] ---------------------------")
#
#    def show_global_combos():
#        print("[COMBOS] (digits 0-9) -------------")
#        if not global_combos:
#            print(" (none)")
#        else:
#            for d in DIGIT_SLOTS:
#                if d in global_combos:
#                    print(f" {d}: {global_combos[d]}")
#                else:
#                    print(f" {d}: (empty)")
#        print("[COMBOS] ---------------------------")
#
#    def dumplist():
#        print("[DUMPS] 0-9 stored snapshots -------")
#        for d in DIGIT_SLOTS:
#            v=i2c_dump_slots.get(d)
#            print(f" {d}: {(str(len(v))+' lines') if v else '(empty)'}")
#        print("[DUMPS] ---------------------------")
#
#    def dump_show(d):
#        v=i2c_dump_slots.get(d)
#        if not v:
#            print(f"[DUMPS] Slot {d} empty"); return
#        print(f"[DUMPS] Slot {d} ({len(v)} lines)")
#        for line in v:
#            print(line)
#
#    HEADER_LINE = "     " + " ".join(f"{i:02x}" for i in range(16))
#    ROW_ADDRS   = [f"{i:02x}" for i in range(0,256,16)]
#
#    def _parse_dump_to_matrix(lines):
#        matrix={}
#        for ln in lines:
#            if ln.startswith("#NO_HEADER#"):
#                continue
#            m=re.match(r'^([0-9A-Fa-f]{2}):\s+(.*)$', ln)
#            if not m: continue
#            addr=m.group(1).lower()
#            rest=m.group(2).strip()
#            bytes_list=[b for b in rest.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', b)]
#            if len(bytes_list)<16:
#                bytes_list += ["--"]*(16-len(bytes_list))
#            elif len(bytes_list)>16:
#                bytes_list=bytes_list[:16]
#            matrix[addr]=[b.upper() for b in bytes_list]
#        for a in ROW_ADDRS:
#            if a not in matrix:
#                matrix[a]=["--"]*16
#        return matrix
#
#    def _hex_to_bin(h):
#        try:
#            bits=f"{int(h,16):08b}"
#            return bits[:4]+"_"+bits[4:]
#        except:
#            return "----_----"
#
#    def _dump_compare_single(a,b, *, suppress_end=False):
#        da=i2c_dump_slots.get(a); db=i2c_dump_slots.get(b)
#        if not da:
#            print(f"[DUMPCMP] Slot {a} empty"); return None
#        if not db:
#            print(f"[DUMPCMP] Slot {b} empty"); return None
#        mA=_parse_dump_to_matrix(da); mB=_parse_dump_to_matrix(db)
#
#        changed_bytes=0
#        changed_rows=0
#
#        print("hex")
#        print(f" disk:{a}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=[]
#            row_changed=False
#            for i in range(16):
#                if rowA[i]==rowB[i]:
#                    line.append("XX")
#                else:
#                    line.append(rowA[i])
#                    changed_bytes+=1
#                    row_changed=True
#            if row_changed: changed_rows+=1
#            print(f"{addr}:  {' '.join(line)}")
#        print(f"disk:{b}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=[]
#            for i in range(16):
#                if rowA[i]==rowB[i]:
#                    line.append("XX")
#                else:
#                    line.append(rowB[i])
#            print(f"{addr}:  {' '.join(line)}")
#
#        print()
#        print("binary")
#        print(f"disk:{a}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=[]
#            for i in range(16):
#                if rowA[i]==rowB[i]:
#                    line.append("XX")
#                else:
#                    line.append(_hex_to_bin(rowA[i]))
#            print(f"{addr}:  {' '.join(line)}")
#        print(f"disk:{b}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=[]
#            for i in range(16):
#                if rowA[i]==rowB[i]:
#                    line.append("XX")
#                else:
#                    line.append(_hex_to_bin(rowB[i]))
#            print(f"{addr}:  {' '.join(line)}")
#        if not suppress_end:
#            print("[DUMPCMP] End")
#        return {"a":a,"b":b,"changed_rows":changed_rows,"changed_bytes":changed_bytes}
#
#    def dump_compare(a,b):
#        return _dump_compare_single(a,b)
#
#    def parse_multi_pairs(arg_str):
#        parts=[p.strip() for p in arg_str.split(",") if p.strip()]
#        pairs=[]
#        for p in parts:
#            toks=p.split()
#            if len(toks)!=2:
#                print(f"[DUMPCMP] Skip invalid pair '{p}'")
#                continue
#            a,b=toks
#            if a in DIGIT_SLOTS and b in DIGIT_SLOTS:
#                pairs.append((a,b))
#            else:
#                print(f"[DUMPCMP] Skip non-digit pair '{p}'")
#        return pairs
#
#    def multi_dump_compare(pairs):
#        if not pairs:
#            print("[DUMPCMP] No valid pairs")
#            return
#        session_stats=[]
#        print(f"[DUMPCMP] Multi compare {len(pairs)} pair(s): {', '.join(f'{a}-{b}' for a,b in pairs)}")
#        for idx,(a,b) in enumerate(pairs,1):
#            print(f"\n[DUMPCMP] Pair {idx}/{len(pairs)} ({a} vs {b})")
#            stats=_dump_compare_single(a,b, suppress_end=True)
#            if stats: print("[DUMPCMP] End")
#            if stats: session_stats.append(stats)
#        entry={
#            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
#            "pairs": [{"a":s["a"],"b":s["b"],"changed_rows":s["changed_rows"],"changed_bytes":s["changed_bytes"]} for s in session_stats]
#        }
#        cmp_history.append(entry)
#        save_cmp_history(DUMPCMP_HISTORY_FILE, cmp_history)
#        total_changed=sum(s["changed_bytes"] for s in session_stats)
#        print(f"\n[DUMPCMP] Multi summary: {len(session_stats)} compared, total changed bytes={total_changed}")
#        for s in session_stats:
#            print(f"  {s['a']} vs {s['b']}: rows={s['changed_rows']} bytes={s['changed_bytes']}")
#
#    def show_cmp_history(limit=10):
#        print("[CMPHIST] Recent multi-compare sessions:")
#        tail=cmp_history[-limit:]
#        if not tail:
#            print(" (none)")
#            return
#        for idx,entry in enumerate(tail,1):
#            ts=entry.get("timestamp","?")
#            pairs_txt=", ".join(f"{p['a']}-{p['b']}:{p['changed_bytes']}" for p in entry.get("pairs",[]))
#            print(f" {idx}. {ts}  {pairs_txt}")
#
#    def show_all():
#        show_slots(); show_global_combos(); dumplist()
#
#    def print_help():
#        print("""[HELP]
#Slots (0-9,a-z):
#  /setX <text>   /comboX <seq>  /enterX  /clrX  oX  /slots  /slotsave  /slotload
#Global combos (0-9):
#  /cset d <seq>  /clist  /crun d  /cclear d  /crun_all  /csave  /cload
#i2cdump capture:
#  /dumpsave d    /dumpshow d    /dumplist
#  /dumpcmp a b
#  /dumpcmp a b,c d,e f   (multi compare pairs, alias: /dumpcmpmulti)
#  /cmpHist [n]   show recent multi-compare history
#Delays & modes:
#  /delay /scriptdelay /linedelay /hex on|off /scriptecho on|off
#General:
#  /help /quit
#Hotkeys (Win):
#  Ctrl+0..9 / Ctrl+a..z play slot
#  Ctrl+S show slots+combos+dumps
#  C+B+digit run digit combo
#  C+L list digit combos
#""")
#
#    def play_slot_recursive(idx_char, depth, visited, script_ctx):
#        if depth>40:
#            print("[PLAY] Depth limit"); return
#        if idx_char not in slot_cmds:
#            print(f"[PLAY] Slot {idx_char} not found"); return
#        v=slot_cmds[idx_char]
#        if v is None:
#            print(f"[PLAY] Slot {idx_char} empty"); return
#        if id(v) in visited:
#            print(f"[PLAY] Cycle at {idx_char}"); return
#        visited.add(id(v))
#        t=v.get("type")
#        if t=="enter":
#            send_enter_only(safe=True, script_ctx=script_ctx)
#        elif t=="combo":
#            for c in v.get("seq",""):
#                if c in slot_cmds:
#                    play_slot_recursive(c, depth+1, visited, script_ctx)
#        else:
#            data=v.get("data","")
#            parts=data.split(TOKEN_ENTER)
#            for pi,segment in enumerate(parts):
#                lines=segment.splitlines()
#                if not lines and segment=="":
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#                for line in lines:
#                    if line.strip()=="" and line!="":
#                        send_enter_only(safe=True, script_ctx=script_ctx)
#                    elif line!="":
#                        send_line(line,safe=True,
#                                  local_echo=f"[RUN] {line}" if SCRIPT_LOCAL_ECHO else None,
#                                  script_ctx=script_ctx)
#                if pi<len(parts)-1:
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#        visited.remove(id(v))
#
#    def play_slot(k):
#        if k not in slot_cmds:
#            print(f"[PLAY] Slot {k} invalid"); return
#        print(f"[PLAY] Slot {k}")
#        ctx=ScriptContext()
#        play_slot_recursive(k,0,set(),ctx)
#
#    def run_global_combo(d):
#        if d not in global_combos:
#            print(f"[COMBO] Digit {d} undefined"); return
#        seq=global_combos[d]; print(f"[COMBO] Run {d}: {seq}")
#        ctx=ScriptContext()
#        for c in seq:
#            if c in slot_cmds:
#                play_slot_recursive(c,0,set(),ctx)
#
#    def run_all_global_combos():
#        defined=[d for d in DIGIT_SLOTS if d in global_combos]
#        if not defined:
#            print("[COMBO] No digit combos defined"); return
#        print("[COMBO] Run ALL digit combos:")
#        ctx=ScriptContext()
#        for d in defined:
#            seq=global_combos[d]; print(f"  -> {d}: {seq}")
#            for c in seq:
#                if c in slot_cmds:
#                    play_slot_recursive(c,0,set(),ctx)
#
#    def run_single_combo_via_hotkey(d):
#        if d in global_combos:
#            print(f"[COMBO] (Hotkey) {d}")
#            run_global_combo(d)
#        else:
#            print(f"[COMBO] (Hotkey) {d} undefined")
#
#    stop_hotkey=threading.Event()
#    hotkey_thread=None
#    if os.name=='nt':
#        try:
#            hotkey_thread=HotkeyThread(
#                play_callback=play_slot,
#                show_all_callback=show_all,
#                combo_list_callback=show_global_combos,
#                run_single_combo_callback=run_single_combo_via_hotkey,
#                stop_event=stop_hotkey
#            )
#            hotkey_thread.start()
#        except Exception as e:
#            print(f"[WARN] Hotkey thread failed: {e}")
#
#    # Command loop
#    try:
#        while True:
#            try: line=input()
#            except EOFError: break
#            stripped=line.strip()
#
#            if stripped=="/help":
#                print_help(); continue
#
#            # i2c dump commands
#            if stripped.startswith("/dumpsave"):
#                parts=stripped.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[DUMPS] Usage: /dumpsave <digit>")
#                else:
#                    d=parts[1]
#                    if _last_captured_dump:
#                        i2c_dump_slots[d]=_last_captured_dump[:]
#                        print(f"[DUMPS] Saved capture to slot {d} ({len(_last_captured_dump)} lines)")
#                        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#                    else:
#                        print("[DUMPS] No captured dump to save")
#                continue
#            if stripped.startswith("/dumpshow"):
#                parts=stripped.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[DUMPS] Usage: /dumpshow <digit>")
#                else:
#                    dump_show(parts[1])
#                continue
#            if stripped=="/dumplist":
#                dumplist(); continue
#
#            if stripped.startswith("/dumpcmp") or stripped.startswith("/dumpcmpmulti"):
#                cmd, *rest = stripped.split(None,1)
#                if not rest:
#                    print("[DUMPCMP] Usage: /dumpcmp a b  OR /dumpcmp a b,c d")
#                    continue
#                rem=rest[0].strip()
#                if ',' in rem or len(rem.split())>2:
#                    if ',' not in rem:
#                        toks=rem.split()
#                        if len(toks)>=4 and len(toks)%2==0:
#                            pair_strs=[f"{toks[i]} {toks[i+1]}" for i in range(0,len(toks),2)]
#                            rem=",".join(pair_strs)
#                    pairs=parse_multi_pairs(rem)
#                    multi_dump_compare(pairs)
#                else:
#                    parts=rem.split()
#                    if len(parts)!=2 or parts[0] not in DIGIT_SLOTS or parts[1] not in DIGIT_SLOTS:
#                        print("[DUMPCMP] Usage: /dumpcmp <a> <b>")
#                    else:
#                        dump_compare(parts[0],parts[1])
#                continue
#
#            if stripped.startswith("/cmpHist"):
#                parts=stripped.split()
#                limit=10
#                if len(parts)==2 and parts[1].isdigit():
#                    limit=max(1,min(100,int(parts[1])))
#                show_cmp_history(limit)
#                continue
#
#            # Delays / modes
#            if stripped.startswith("/delay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[DELAY] {char_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        char_delay=v; print(f"[DELAY] -> {char_delay} ms"); persist_user()
#                    except: print(f"[DELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptdelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTDELAY] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[SCRIPTDELAY] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
#                    except: print(f"[SCRIPTDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/linedelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[LINEDELAY] {line_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        line_delay=v; print(f"[LINEDELAY] -> {line_delay} ms"); persist_user()
#                    except: print(f"[LINEDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptecho"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTECHO] {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        SCRIPT_LOCAL_ECHO=(arg=="on"); print(f"[SCRIPTECHO] -> {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}"); persist_user()
#                    else: print("[SCRIPTECHO] Use: /scriptecho on|off")
#                continue
#            if stripped.startswith("/hex"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[HEX] {'ON' if TX_HEX else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        TX_HEX=(arg=="on"); print(f"[HEX] -> {'ON' if TX_HEX else 'OFF'}"); persist_user()
#                    else: print("[HEX] Use: /hex on|off")
#                continue
#
#            # Slots persistence
#            if stripped=="/slotsave":
#                save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
#            if stripped=="/slotload":
#                load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue
#
#            # Combos
#            if stripped=="/clist":
#                show_global_combos(); continue
#            if stripped.startswith("/cset "):
#                parts=stripped.split(None,2)
#                if len(parts)<3:
#                    print("[COMBO] Usage: /cset <digit> <seq>")
#                else:
#                    digit=parts[1]
#                    if not (digit.isdigit() and len(digit)==1):
#                        print("[COMBO] Name must be a single digit (0-9)")
#                    else:
#                        seq="".join(ch for ch in parts[2] if ch.isalnum())
#                        global_combos[digit]=seq
#                        print(f"[COMBO] {digit} = {seq}")
#                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                continue
#            if stripped.startswith("/crun "):
#                digit=stripped.split(None,1)[1].strip()
#                run_global_combo(digit); continue
#            if stripped.startswith("/cclear "):
#                digit=stripped.split(None,1)[1].strip()
#                if digit in global_combos:
#                    del global_combos[digit]; print(f"[COMBO] Cleared {digit}")
#                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                else:
#                    print(f"[COMBO] {digit} not defined")
#                continue
#            if stripped=="/csave":
#                save_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/cload":
#                load_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/crun_all":
#                run_all_global_combos(); continue
#
#            # General
#            if stripped=="/quit":
#                print("[INFO] Quit")
#                break
#            if stripped=="/slots":
#                show_slots(); continue
#
#            # Slot definitions
#            if stripped.startswith("/enter") and len(stripped)==7:
#                key=stripped[6].lower()
#                if key in slot_cmds:
#                    slot_cmds[key]={"type":"enter"}
#                    print(f"[SET] Slot {key} = <ENTER>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/combo") and len(stripped)>=7:
#                key=stripped[6].lower()
#                if key in slot_cmds:
#                    parts=line.split(None,1)
#                    seq=""
#                    if len(parts)>1:
#                        seq="".join(ch for ch in parts[1] if ch.isalnum())
#                    slot_cmds[key]={"type":"combo","seq":seq}
#                    print(f"[SET] Slot {key} = <COMBO {seq}>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/set") and len(stripped)>=5:
#                key=stripped[4].lower()
#                if key in slot_cmds:
#                    parts=line.split(None,1)
#                    data=parts[1] if len(parts)>1 else ""
#                    slot_cmds[key]={"type":"raw","data":data}
#                    print(f"[SET] Slot {key} raw length={len(data)}")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/clr") and len(stripped)==5:
#                key=stripped[4].lower()
#                if key in slot_cmds:
#                    slot_cmds[key]=None
#                    print(f"[CLR] Slot {key} cleared")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            # Play slot
#            if len(stripped)==2 and stripped[0] in ('o','O'):
#                key=stripped[1].lower()
#                if key in slot_cmds:
#                    play_slot(key)
#                continue
#
#            # Blank line => ENTER
#            if line=="":
#                send_enter_only(safe=False)
#                continue
#
#            # Normal input
#            try:
#                body=line.encode(ENCODING,errors="replace")
#            except Exception as e:
#                print(f"[WARN] Encode failed: {e}")
#                continue
#            send_bytes(body+line_suffix(), safe=False, tag="TX")
#
#    except KeyboardInterrupt:
#        print("\n[INFO] KeyboardInterrupt")
#    finally:
#        persist_user()
#        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#        save_cmp_history(DUMPCMP_HISTORY_FILE, cmp_history)
#        if 'hotkey_thread' in locals() and hotkey_thread:
#            stop_hotkey.set()
#            hotkey_thread.join(timeout=0.5)
#        reader.stop()
#        time.sleep(0.05)
#        try: ser.close()
#        except: pass
#        if 'log_file' in locals() and log_file:
#            try: log_file.close()
#            except: pass
#        print("[INFO] Exit")
#
#if __name__ == "__main__":
#    main()
#
#    """
























    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
250904_0001_i2cdump_data_compare_pass
250904_0002_i2cdump_data_compare_binary_pass

    """



#    """
##!/usr/bin/env python
## -*- coding: utf-8 -*-
#"""
#Serial console with:
# - Slots 0-9 + a-z (36 total)
# - Digit global combos (0-9)
# - Hotkeys: Ctrl+0..9 / Ctrl+a..z (play slot), Ctrl+S (show all), C+B+<digit> (single combo), C+L (list combos)
# - i2cdump capture & storage (/dumpsave /dumpshow /dumplist /dumpcmp)
# - Tolerant i2cdump capture (header or first data row, prompt line, overflow guard)
# - /dumpcmp:
#     hex
#       disk:<a> (unchanged => XX, changed => HEX from A)
#       disk:<b> (unchanged => XX, changed => HEX from B)
#     binary
#       disk:<a> row lines (unchanged => XX, changed => 8-bit binary xxxx_xxxx)
#       disk:<b> row lines (same rule)
#     (Binary section no longer prints extra "<addr>b:" lines; changed bytes are shown
#      inline as binary in the same row. Receiver thread kept unchanged except for dump
#      feed hook.)
#"""
#
#import sys
#import serial
#import threading
#import time
#import os
#import json
#import re
#from datetime import datetime
#
#try:
#    from serial.tools import list_ports
#except ImportError:
#    list_ports = None
#
## ================== Config (overridden by saved user config) ==================
#PORT                    = "COM5"
#BAUD                    = 115200
#PARITY_NAME             = "none"
#DATA_BITS               = 8
#STOP_BITS               = 1
#FLOW_CTRL               = "none"
#ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
#ENCODING                = "utf-8"
#TIMEOUT                 = 0.05
#CHAR_DELAY_MS           = 0
#LINE_DELAY_MS           = 0
#ASSERT_DTR              = False
#ASSERT_RTS              = False
#CLEAR_BUFF_ON_OPEN      = False
#
#TX_HEX                  = True
#HEX_DUMP_RX             = False
#RAW_RX                  = False
#QUIET_RX                = False
#
#LOG_PATH                = None
#INI_PATH                = None
#NO_BANNER               = False
#
#INTERACTIVE_SELECT      = True
#REMEMBER_LAST           = True
#LAST_FILE_NAME          = ".last_port"
#
#SLOTS_SAVE_FILE         = ".slot_cmds.json"
#AUTO_SAVE_SLOTS         = True
#SHOW_SAVE_MESSAGE       = True
#
#COMBO_SAVE_FILE         = ".combo_defs.json"
#AUTO_SAVE_COMBOS        = True
#SHOW_COMBO_SAVE_MSG     = True
#
#USER_CONFIG_FILE        = ".console_config.json"
#AUTO_SAVE_CONFIG        = True
#
#I2C_DUMP_SAVE_FILE      = ".i2c_dumps.json"
#AUTO_SAVE_I2C_DUMPS     = True
#MAX_I2C_DUMPS           = 10   # 0-9
#
#SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
#SCRIPT_LOCAL_ECHO         = False
#
#PROMPT_PATTERN            = "i2c>"
#SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
#SCRIPT_WAIT_PROMPT        = True
#POST_PROMPT_STABILIZE_MS  = 5
#
#HOTKEY_POLL_INTERVAL_SEC  = 0.05
#TOKEN_ENTER               = "<ENTER>"
#
#DIGIT_SLOTS  = [str(i) for i in range(10)]
#LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
#ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS
#
## ======================================================================
## Utility
## ======================================================================
#def format_hex(data: bytes) -> str:
#    return " ".join(f"{b:02X}" for b in data)
#
#def parse_ini(path: str):
#    out = {}
#    if not path or not os.path.isfile(path):
#        return out
#    try:
#        with open(path, "r", encoding="utf-8", errors="ignore") as f:
#            for line in f:
#                line=line.strip()
#                if not line or line.startswith(";") or "=" not in line:
#                    continue
#                k,v=line.split("=",1)
#                k=k.strip(); v=v.strip()
#                kl=k.lower()
#                if kl in ("comport","baudrate","delayperchar","delayperline"):
#                    try: out[k]=int(v)
#                    except: pass
#                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
#                    out[k]=v
#    except Exception as e:
#        print(f"[WARN] INI parse failed: {e}")
#    return out
#
#def load_user_config():
#    if not os.path.isfile(USER_CONFIG_FILE):
#        return {}
#    try:
#        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        return data if isinstance(data,dict) else {}
#    except Exception as e:
#        print(f"[CFG] Load failed: {e}")
#        return {}
#
#def save_user_config(cfg):
#    if not AUTO_SAVE_CONFIG: return
#    try:
#        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
#            json.dump(cfg,f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[CFG] Save failed: {e}")
#
#def normalize_slot_value(v):
#    if v is None: return None
#    if isinstance(v,dict):
#        t=v.get("type")
#        if t=="raw":
#            d=v.get("data"); return {"type":"raw","data": d if isinstance(d,str) else ""}
#        if t=="enter": return {"type":"enter"}
#        if t=="combo":
#            seq=v.get("seq","")
#            if not isinstance(seq,str): seq=""
#            return {"type":"combo","seq":seq}
#        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
#    if isinstance(v,str): return {"type":"raw","data":v}
#    return {"type":"raw","data":str(v)}
#
#def load_slots_from_file(path, slot_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        changed=False
#        for k in slot_dict.keys():
#            if k in data:
#                slot_dict[k]=normalize_slot_value(data[k]); changed=True
#        if changed: print(f"[SLOTS] Loaded {path}")
#    except Exception as e:
#        print(f"[SLOTS] Load failed: {e}")
#
#def save_slots_to_file(path, slot_dict):
#    try:
#        out={k:(None if v is None else v) for k,v in slot_dict.items()}
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(out,f,ensure_ascii=False,indent=2)
#        if SHOW_SAVE_MESSAGE: print(f"[SLOTS] Saved -> {path}")
#    except Exception as e:
#        print(f"[SLOTS] Save failed: {e}")
#
#def load_global_combos(path, combo_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
#                    combo_dict[k]="".join(ch for ch in v if ch.isalnum())
#            print(f"[COMBO] Loaded {path} ({len(combo_dict)} items)")
#    except Exception as e:
#        print(f"[COMBO] Load failed: {e}")
#
#def save_global_combos(path, combo_dict):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
#        if SHOW_COMBO_SAVE_MSG: print(f"[COMBO] Saved -> {path}")
#    except Exception as e:
#        print(f"[COMBO] Save failed: {e}")
#
#def load_i2c_dumps(path, dump_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if k in dump_dict and isinstance(v,list):
#                    dump_dict[k]=v
#        print(f"[DUMPS] Loaded {path}")
#    except Exception as e:
#        print(f"[DUMPS] Load failed: {e}")
#
#def save_i2c_dumps(path, dump_dict):
#    if not AUTO_SAVE_I2C_DUMPS: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(dump_dict,f,ensure_ascii=False,indent=2)
#        print(f"[DUMPS] Saved -> {path}")
#    except Exception as e:
#        print(f"[DUMPS] Save failed: {e}")
#
## ======================================================================
## Prompt tracking
## ======================================================================
#prompt_lock=threading.Lock()
#prompt_seq=0
#def inc_prompt_if_in(text:str):
#    global prompt_seq
#    if PROMPT_PATTERN and PROMPT_PATTERN in text:
#        with prompt_lock:
#            prompt_seq+=1
#def get_prompt_seq():
#    with prompt_lock:
#        return prompt_seq
#def wait_for_next_prompt(prev_seq, timeout):
#    if not SCRIPT_WAIT_PROMPT: return prev_seq
#    deadline=time.time()+timeout
#    while time.time()<deadline:
#        cur=get_prompt_seq()
#        if cur>prev_seq:
#            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
#            return cur
#        time.sleep(0.01)
#    return get_prompt_seq()
#
## ======================================================================
## i2cdump capture logic
## ======================================================================
#_i2c_capture_buffer_fragment=""
#_i2c_capture_active=False
#_i2c_capture_lines=[]
#_last_captured_dump=None
#
#_I2C_HEADER_RE = re.compile(r'^\s+00(?:\s+[0-9A-Fa-f]{2}){15}\s*$')
#_I2C_DATA_ROW_RE = re.compile(r'^[0-9A-Fa-f]{2}:\s+([0-9A-Fa-f]{2}\s+){0,15}[0-9A-Fa-f]{2}\s*$')
#_LAST_ADDR = "f0"
#
#def _maybe_finalize_partial(reason:str):
#    global _i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if _i2c_capture_active and _i2c_capture_lines:
#        _last_captured_dump=_i2c_capture_lines[:]
#        print(f"\n[DUMPS] Captured ({reason}) {len(_last_captured_dump)} lines")
#    _i2c_capture_active=False
#    _i2c_capture_lines=[]
#
#def _i2c_capture_feed(chunk:str):
#    global _i2c_capture_buffer_fragment,_i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if not chunk: return
#    _i2c_capture_buffer_fragment += chunk
#    while True:
#        if '\n' not in _i2c_capture_buffer_fragment:
#            break
#        line,rest=_i2c_capture_buffer_fragment.split('\n',1)
#        _i2c_capture_buffer_fragment=rest
#        line=line.rstrip('\r')
#        if PROMPT_PATTERN and line.startswith(PROMPT_PATTERN):
#            if _i2c_capture_active:
#                _maybe_finalize_partial("prompt")
#            continue
#        if not _i2c_capture_active:
#            if _I2C_HEADER_RE.match(line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=[line]
#                continue
#            if re.match(r'^00:\s', line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=["#NO_HEADER#"]
#            else:
#                continue
#        if _i2c_capture_active:
#            if _I2C_DATA_ROW_RE.match(line) or line==_i2c_capture_lines[0]:
#                if line != _i2c_capture_lines[0]:
#                    _i2c_capture_lines.append(line)
#            else:
#                if line.strip():
#                    _i2c_capture_lines.append(line)
#            if line.lower().startswith(_LAST_ADDR + ":"):
#                _last_captured_dump=_i2c_capture_lines[:]
#                print(f"\n[DUMPS] Captured i2cdump ({len(_last_captured_dump)} lines)")
#                _i2c_capture_active=False
#                _i2c_capture_lines=[]
#                continue
#            if len(_i2c_capture_lines) > 60:
#                _maybe_finalize_partial("overflow")
#                continue
#
## ======================================================================
## Receiver thread
## ======================================================================
#class SerialReaderThread(threading.Thread):
#    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
#        super().__init__(daemon=True)
#        self.ser=ser; self.encoding=encoding
#        self.hex_dump=hex_dump; self.raw=raw
#        self.log_file=log_file; self.quiet=quiet
#        self._running=True
#    def stop(self): self._running=False
#    def run(self):
#        while self._running and self.ser.is_open:
#            try:
#                data=self.ser.read(self.ser.in_waiting or 1)
#            except serial.SerialException as e:
#                print(f"[ERR] Serial exception: {e}")
#                break
#            if not data: continue
#            if self.log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try:
#                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n"); self.log_file.flush()
#                except Exception: pass
#            if self.quiet: continue
#            if self.hex_dump:
#                txt=format_hex(data)
#                print(f"[RX HEX] {txt}")
#                inc_prompt_if_in(txt)
#                _i2c_capture_feed(txt+"\n")
#            elif self.raw:
#                sys.stdout.buffer.write(data); sys.stdout.flush()
#                try:
#                    decoded=data.decode(self.encoding,errors="ignore")
#                    inc_prompt_if_in(decoded)
#                    _i2c_capture_feed(decoded)
#                except: pass
#            else:
#                try:
#                    text=data.decode(self.encoding,errors="replace")
#                except Exception:
#                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
#                print(text,end="",flush=True)
#                inc_prompt_if_in(text)
#                _i2c_capture_feed(text)
#
## ======================================================================
## Port selection
## ======================================================================
#def load_last_port():
#    if not REMEMBER_LAST: return None
#    try:
#        if os.path.isfile(LAST_FILE_NAME):
#            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
#                v=f.read().strip()
#                if v: return v
#    except: pass
#    return None
#def save_last_port(p):
#    if not REMEMBER_LAST: return
#    try:
#        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
#            f.write(p.strip())
#    except: pass
#
#def interactive_select_port(default_port):
#    port=default_port; baud=BAUD; parity_name=PARITY_NAME
#    data_bits=DATA_BITS; stop_bits=STOP_BITS; flow_ctrl=FLOW_CTRL; enter_mode=ENTER_MODE
#    last=load_last_port()
#    if last: default_port=last
#    if not INTERACTIVE_SELECT:
#        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#    print("=== Serial Interactive Config (Enter to keep default) ===")
#    if list_ports:
#        ports=list(list_ports.comports())
#        if ports:
#            print("Available ports:")
#            for idx,p in enumerate(ports,1):
#                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
#        else:
#            print("No COM ports detected.")
#    val=input(f"Port [{default_port}]: ").strip()
#    if val: port=val
#    val=input(f"Baud [{baud}]: ").strip()
#    if val.isdigit(): baud=int(val)
#    plist=["none","even","odd","mark","space"]
#    val=input(f"Parity {plist} [{parity_name}]: ").strip().lower()
#    if val in plist: parity_name=val
#    val=input(f"Data bits (7/8) [{data_bits}]: ").strip()
#    if val in ("7","8"): data_bits=int(val)
#    val=input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
#    if val in ("1","2"): stop_bits=int(val)
#    flist=["none","rtscts","dsrdtr","x"]
#    val=input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
#    if val in flist: flow_ctrl=val
#    emlist=["CR","CRLF","LF","NONE"]
#    val=input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
#    if val in emlist: enter_mode=val
#    save_last_port(port)
#    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#
## ======================================================================
## Hotkey Thread
## ======================================================================
#class HotkeyThread(threading.Thread):
#    def __init__(self,
#                 play_callback,
#                 show_all_callback,
#                 combo_list_callback,
#                 run_single_combo_callback,
#                 stop_event):
#        super().__init__(daemon=True)
#        self.play_callback=play_callback
#        self.show_all_callback=show_all_callback
#        self.combo_list_callback=combo_list_callback
#        self.run_single_combo_callback=run_single_combo_callback
#        self.stop_event=stop_event
#        import ctypes
#        self.ctypes=ctypes
#        self.user32=ctypes.WinDLL("user32", use_last_error=True)
#        self.VK_CTRL=0x11; self.VK_S=0x53
#        self.VK_C=0x43; self.VK_B=0x42; self.VK_L=0x4C
#        self.VK_0_9=list(range(0x30,0x3A))
#        self.VK_NUM_0_9=list(range(0x60,0x6A))
#        self.VK_A_Z=list(range(0x41,0x5B))
#        self.prev_digit_down={vk:False for vk in self.VK_0_9+self.VK_NUM_0_9}
#        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
#        self.prev_s_down=False
#        self.prev_cb=False
#        self.prev_cl_combo_list=False
#    def key_down(self,vk):
#        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
#    def run(self):
#        while not self.stop_event.is_set():
#            ctrl=self.key_down(self.VK_CTRL)
#            s_now=ctrl and self.key_down(self.VK_S)
#            if s_now and not self.prev_s_down:
#                print(); self.show_all_callback()
#            self.prev_s_down=s_now
#            c_now=self.key_down(self.VK_C); b_now=self.key_down(self.VK_B)
#            cb_now=c_now and b_now
#            if cb_now:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        self.run_single_combo_callback(key)
#                    self.prev_digit_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#            self.prev_cb=cb_now
#            l_now=self.key_down(self.VK_L); cl_now=c_now and l_now
#            if cl_now and not self.prev_cl_combo_list:
#                print(); self.combo_list_callback()
#            self.prev_cl_combo_list=cl_now
#            if ctrl:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        print(); self.play_callback(key.lower())
#                    self.prev_digit_down[vk]=now
#                for vk in self.VK_A_Z:
#                    now=self.key_down(vk)
#                    if now and not self.prev_letter_down[vk]:
#                        key=chr(vk).lower()
#                        print(); self.play_callback(key)
#                    self.prev_letter_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
#                self.prev_s_down=False
#            time.sleep(HOTKEY_POLL_INTERVAL_SEC)
#
## ======================================================================
## Main
## ======================================================================
#def main():
#    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO, _last_captured_dump
#    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
#    user_cfg=load_user_config()
#
#    if "char_delay_ms" in user_cfg:
#        try: globals()['CHAR_DELAY_MS']=float(user_cfg["char_delay_ms"])
#        except: pass
#    if "line_delay_ms" in user_cfg:
#        try: globals()['LINE_DELAY_MS']=float(user_cfg["line_delay_ms"])
#        except: pass
#    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
#    if "script_char_delay_ms" in user_cfg:
#        try:
#            v=float(user_cfg["script_char_delay_ms"])
#            if v>=0: SAFE_SCRIPT_CHAR_DELAY_MS=v
#        except: pass
#    if "script_local_echo" in user_cfg:
#        SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
#
#    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
#    init_baud=cfg_ini.get("BaudRate",BAUD)
#    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
#    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
#    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
#    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
#    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
#    if init_enter not in ("CR","CRLF","LF","NONE"): init_enter="CR"
#
#    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)
#
#    parity_map={"even":serial.PARITY_EVEN,"odd":serial.PARITY_ODD,"none":serial.PARITY_NONE,
#                "mark":serial.PARITY_MARK,"space":serial.PARITY_SPACE}
#    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
#    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
#    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE
#
#    if fc in ("rtscts","hard"):
#        rtscts,dsrdtr,xonxoff=True,False,False
#    elif fc=="dsrdtr":
#        rtscts,dsrdtr,xonxoff=False,True,False
#    elif fc=="x":
#        rtscts,dsrdtr,xonxoff=False,False,True
#    else:
#        rtscts=dsrdtr=xonxoff=False
#
#    char_delay=float(globals()['CHAR_DELAY_MS']); line_delay=float(globals()['LINE_DELAY_MS'])
#
#    try:
#        ser=serial.Serial(port,baud,timeout=TIMEOUT,
#                          bytesize=bytesize,parity=parity,stopbits=stopbits,
#                          rtscts=rtscts,dsrdtr=dsrdtr,xonxoff=xonxoff,write_timeout=1)
#    except serial.SerialException as e:
#        print(f"[ERR] Cannot open {port}: {e}"); return
#
#    try:
#        if ASSERT_DTR: ser.setDTR(True)
#        if ASSERT_RTS: ser.setRTS(True)
#    except Exception as e:
#        print(f"[WARN] Setting DTR/RTS failed: {e}")
#
#    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
#        try:
#            ser.reset_input_buffer(); ser.reset_output_buffer()
#        except Exception as e: print(f"[WARN] Clear buffers failed: {e}")
#
#    if not NO_BANNER:
#        print(f"[INFO] Opened {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
#        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
#        print(f"[INFO] Enter={enter_mode} char_delay={char_delay}ms line_delay={line_delay}ms script_min={SAFE_SCRIPT_CHAR_DELAY_MS}ms hex={'ON' if TX_HEX else 'OFF'} echo={'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#        print("[INFO] Type /help for command list.")
#
#    log_file=None
#    if LOG_PATH:
#        try:
#            log_file=open(LOG_PATH,"a",encoding="utf-8")
#            print(f"[INFO] Logging to {LOG_PATH}")
#        except Exception as e:
#            print(f"[WARN] Log open failed: {e}")
#
#    reader=SerialReaderThread(
#        ser,encoding=ENCODING,hex_dump=HEX_DUMP_RX,raw=RAW_RX,
#        log_file=log_file,quiet=QUIET_RX
#    )
#    reader.start()
#
#    send_lock=threading.Lock()
#
#    def persist_user():
#        user_cfg["char_delay_ms"]=char_delay
#        user_cfg["line_delay_ms"]=line_delay
#        user_cfg["tx_hex"]=TX_HEX
#        user_cfg["script_char_delay_ms"]=SAFE_SCRIPT_CHAR_DELAY_MS
#        user_cfg["script_local_echo"]=SCRIPT_LOCAL_ECHO
#        save_user_config(user_cfg)
#
#    def line_suffix():
#        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]
#
#    def send_bytes(data:bytes, tag="TX", safe=False, local_echo_line=None):
#        if not data: return
#        per_char_delay=char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
#        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
#            print(local_echo_line)
#        if per_char_delay>0 and len(data)>1:
#            for i,b in enumerate(data):
#                with send_lock:
#                    try: ser.write(bytes([b])); ser.flush()
#                    except serial.SerialException as e:
#                        print(f"[ERR] TX failed: {e}"); return
#                if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(bytes([b]))}")
#                if log_file:
#                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
#                    except: pass
#                if i<len(data)-1: time.sleep(per_char_delay/1000.0)
#        else:
#            with send_lock:
#                try: ser.write(data); ser.flush()
#                except serial.SerialException as e:
#                    print(f"[ERR] TX failed: {e}"); return
#            if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(data)}")
#            if log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
#                except: pass
#        if line_delay>0 and tag.startswith("TX"): time.sleep(line_delay/1000.0)
#
#    class ScriptContext:
#        def __init__(self):
#            self.last_prompt_seq=get_prompt_seq(); self.first_send=True
#        def wait_ready_if_needed(self):
#            if not SCRIPT_WAIT_PROMPT: return
#            if self.first_send:
#                self.first_send=False; return
#            prev=self.last_prompt_seq
#            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
#        def note_after_send(self): pass
#
#    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        try: body=text.encode(ENCODING,errors="replace")
#        except Exception as e: print(f"[WARN] Encode failed: {e}"); return
#        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    def send_enter_only(safe=False, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        send_bytes(line_suffix(), tag="TX-EMPTY", safe=safe)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    slot_cmds={k:None for k in ALL_SLOTS}; load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
#    global_combos={}; load_global_combos(COMBO_SAVE_FILE, global_combos)
#
#    i2c_dump_slots={str(i):None for i in range(MAX_I2C_DUMPS)}
#    load_i2c_dumps(I2C_DUMP_SAVE_FILE, i2c_dump_slots)
#
#    def show_slots():
#        print("[SLOTS] ---------------------------")
#        for k in DIGIT_SLOTS+LETTER_SLOTS:
#            v=slot_cmds.get(k)
#            if v is None: print(f" {k}: (empty)")
#            else:
#                t=v.get("type")
#                if t=="enter": print(f" {k}: <ENTER>")
#                elif t=="combo": print(f" {k}: <COMBO {v.get('seq','')}>")
#                else:
#                    data=v.get("data","")
#                    first=data.splitlines()[0] if data else ""
#                    more=" ..." if "\n" in data else ""
#                    print(f" {k}: {first[:60]}{more}")
#        print("[SLOTS] ---------------------------")
#
#    def show_global_combos():
#        print("[COMBOS] (digits 0-9) -------------")
#        if not global_combos: print(" (none)")
#        else:
#            for d in DIGIT_SLOTS:
#                if d in global_combos: print(f" {d}: {global_combos[d]}")
#                else: print(f" {d}: (empty)")
#        print("[COMBOS] ---------------------------")
#
#    def dumplist():
#        print("[DUMPS] 0-9 stored snapshots -------")
#        for d in DIGIT_SLOTS:
#            v=i2c_dump_slots.get(d)
#            print(f" {d}: {(str(len(v))+' lines') if v else '(empty)'}")
#        print("[DUMPS] ---------------------------")
#
#    def dump_show(d):
#        v=i2c_dump_slots.get(d)
#        if not v:
#            print(f"[DUMPS] Slot {d} empty"); return
#        print(f"[DUMPS] Slot {d} ({len(v)} lines)")
#        for line in v:
#            print(line)
#
#    HEADER_LINE = "     " + " ".join(f"{i:02x}" for i in range(16))
#    ROW_ADDRS   = [f"{i:02x}" for i in range(0,256,16)]
#
#    def _parse_dump_to_matrix(lines):
#        matrix={}
#        for ln in lines:
#            if ln.startswith("#NO_HEADER#"):
#                continue
#            m=re.match(r'^([0-9A-Fa-f]{2}):\s+(.*)$', ln)
#            if not m: continue
#            addr=m.group(1).lower()
#            rest=m.group(2).strip()
#            bytes_list=[b for b in rest.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', b)]
#            if len(bytes_list)<16:
#                bytes_list += ["--"]*(16-len(bytes_list))
#            elif len(bytes_list)>16:
#                bytes_list=bytes_list[:16]
#            matrix[addr]=[b.upper() for b in bytes_list]
#        for a in ROW_ADDRS:
#            if a not in matrix:
#                matrix[a]=["--"]*16
#        return matrix
#
#    def _hex_to_bin(h):
#        try:
#            bits=f"{int(h,16):08b}"
#            return bits[:4]+"_"+bits[4:]
#        except:
#            return "----_----"
#
#    def dump_compare(a,b):
#        da=i2c_dump_slots.get(a); db=i2c_dump_slots.get(b)
#        if not da: print(f"[DUMPCMP] Slot {a} empty"); return
#        if not db: print(f"[DUMPCMP] Slot {b} empty"); return
#        mA=_parse_dump_to_matrix(da); mB=_parse_dump_to_matrix(db)
#
#        print("hex")
#        print(f" disk:{a}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=["XX" if rowA[i]==rowB[i] else rowA[i] for i in range(16)]
#            print(f"{addr}:  {' '.join(line)}")
#        print(f"disk:{b}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=["XX" if rowA[i]==rowB[i] else rowB[i] for i in range(16)]
#            print(f"{addr}:  {' '.join(line)}")
#
#        print()
#        print("binary")
#        print(f"disk:{a}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowA[i]) for i in range(16)]
#            print(f"{addr}:  {' '.join(line)}")
#        print(f"disk:{b}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            line=["XX" if rowA[i]==rowB[i] else _hex_to_bin(rowB[i]) for i in range(16)]
#            print(f"{addr}:  {' '.join(line)}")
#        print("[DUMPCMP] End")
#
#    def show_all():
#        show_slots(); show_global_combos(); dumplist()
#
#    def print_help():
#        print("""[HELP]
#Slots (0-9,a-z):
#  /setX <text>   /comboX <seq>  /enterX  /clrX  oX  /slots  /slotsave  /slotload
#Global combos (0-9):
#  /cset d <seq>  /clist  /crun d  /cclear d  /crun_all  /csave  /cload
#i2cdump capture:
#  /dumpsave d    /dumpshow d    /dumplist
#  /dumpcmp a b   (hex diff + binary diff; unchanged=XX; changed -> binary)
#Delays & modes:
#  /delay /scriptdelay /linedelay /hex on|off /scriptecho on|off
#General:
#  /help /quit
#Hotkeys (Win):
#  Ctrl+0..9 / Ctrl+a..z play slot
#  Ctrl+S show slots+combos+dumps
#  C+B+digit run digit combo
#  C+L list digit combos
#""")
#
#    def play_slot_recursive(idx_char, depth, visited, script_ctx):
#        if depth>40: print("[PLAY] Depth limit"); return
#        if idx_char not in slot_cmds: print(f"[PLAY] Slot {idx_char} not found"); return
#        v=slot_cmds[idx_char]
#        if v is None: print(f"[PLAY] Slot {idx_char} empty"); return
#        if id(v) in visited: print(f"[PLAY] Cycle at {idx_char}"); return
#        visited.add(id(v))
#        t=v.get("type")
#        if t=="enter":
#            send_enter_only(safe=True, script_ctx=script_ctx)
#        elif t=="combo":
#            for c in v.get("seq",""):
#                if c in slot_cmds: play_slot_recursive(c, depth+1, visited, script_ctx)
#        else:
#            data=v.get("data","")
#            parts=data.split(TOKEN_ENTER)
#            for pi,segment in enumerate(parts):
#                lines=segment.splitlines()
#                if not lines and segment=="":
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#                for line in lines:
#                    if line.strip()=="" and line!="":
#                        send_enter_only(safe=True, script_ctx=script_ctx)
#                    elif line!="":
#                        send_line(line,safe=True,
#                                  local_echo=f"[RUN] {line}" if SCRIPT_LOCAL_ECHO else None,
#                                  script_ctx=script_ctx)
#                if pi<len(parts)-1:
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#        visited.remove(id(v))
#
#    def play_slot(k):
#        if k not in slot_cmds:
#            print(f"[PLAY] Slot {k} invalid"); return
#        print(f"[PLAY] Slot {k}")
#        ctx=ScriptContext()
#        play_slot_recursive(k,0,set(),ctx)
#
#    def run_global_combo(d):
#        if d not in global_combos:
#            print(f"[COMBO] Digit {d} undefined"); return
#        seq=global_combos[d]; print(f"[COMBO] Run {d}: {seq}")
#        ctx=ScriptContext()
#        for c in seq:
#            if c in slot_cmds: play_slot_recursive(c,0,set(),ctx)
#
#    def run_all_global_combos():
#        defined=[d for d in DIGIT_SLOTS if d in global_combos]
#        if not defined:
#            print("[COMBO] No digit combos defined"); return
#        print("[COMBO] Run ALL digit combos:")
#        ctx=ScriptContext()
#        for d in defined:
#            seq=global_combos[d]; print(f"  -> {d}: {seq}")
#            for c in seq:
#                if c in slot_cmds: play_slot_recursive(c,0,set(),ctx)
#
#    def run_single_combo_via_hotkey(d):
#        if d in global_combos:
#            print(f"[COMBO] (Hotkey) {d}")
#            run_global_combo(d)
#        else:
#            print(f"[COMBO] (Hotkey) {d} undefined")
#
#    stop_hotkey=threading.Event()
#    hotkey_thread=None
#    if os.name=='nt':
#        try:
#            hotkey_thread=HotkeyThread(
#                play_callback=play_slot,
#                show_all_callback=show_all,
#                combo_list_callback=show_global_combos,
#                run_single_combo_callback=run_single_combo_via_hotkey,
#                stop_event=stop_hotkey
#            ); hotkey_thread.start()
#        except Exception as e:
#            print(f"[WARN] Hotkey thread failed: {e}")
#
#    # Command loop
#    try:
#        while True:
#            try: line=input()
#            except EOFError: break
#            stripped=line.strip()
#
#            if stripped=="/help": print_help(); continue
#
#            # i2cdump commands
#            if stripped.startswith("/dumpsave"):
#                parts=stripped.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[DUMPS] Usage: /dumpsave <digit>")
#                else:
#                    d=parts[1]
#                    if _last_captured_dump:
#                        i2c_dump_slots[d]=_last_captured_dump[:]
#                        print(f"[DUMPS] Saved capture to slot {d} ({len(_last_captured_dump)} lines)")
#                        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#                    else:
#                        print("[DUMPS] No captured dump to save")
#                continue
#            if stripped.startswith("/dumpshow"):
#                parts=stripped.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[DUMPS] Usage: /dumpshow <digit>")
#                else:
#                    dump_show(parts[1])
#                continue
#            if stripped.startswith("/dumpcmp"):
#                parts=stripped.split()
#                if len(parts)!=3 or parts[1] not in DIGIT_SLOTS or parts[2] not in DIGIT_SLOTS:
#                    print("[DUMPCMP] Usage: /dumpcmp <a> <b>")
#                else:
#                    dump_compare(parts[1],parts[2])
#                continue
#            if stripped=="/dumplist":
#                dumplist(); continue
#
#            # Delays / modes
#            if stripped.startswith("/delay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[DELAY] {char_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0; char_delay=v; print(f"[DELAY] -> {char_delay} ms"); persist_user()
#                    except: print(f"[DELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptdelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTDELAY] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0; SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[SCRIPTDELAY] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
#                    except: print(f"[SCRIPTDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/linedelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[LINEDELAY] {line_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0; line_delay=v; print(f"[LINEDELAY] -> {line_delay} ms"); persist_user()
#                    except: print(f"[LINEDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptecho"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTECHO] {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        SCRIPT_LOCAL_ECHO=(arg=="on"); print(f"[SCRIPTECHO] -> {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}"); persist_user()
#                    else:
#                        print("[SCRIPTECHO] Use: /scriptecho on|off")
#                continue
#            if stripped.startswith("/hex"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[HEX] {'ON' if TX_HEX else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        TX_HEX=(arg=="on"); print(f"[HEX] -> {'ON' if TX_HEX else 'OFF'}"); persist_user()
#                    else:
#                        print("[HEX] Use: /hex on|off")
#                continue
#
#            # Slots persistence
#            if stripped=="/slotsave": save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
#            if stripped=="/slotload": load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue
#
#            # Combos
#            if stripped=="/clist": show_global_combos(); continue
#            if stripped.startswith("/cset "):
#                parts=stripped.split(None,2)
#                if len(parts)<3: print("[COMBO] Usage: /cset <digit> <seq>")
#                else:
#                    digit=parts[1]
#                    if not (digit.isdigit() and len(digit)==1):
#                        print("[COMBO] Name must be single digit (0-9)")
#                    else:
#                        seq="".join(ch for ch in parts[2] if ch.isalnum())
#                        global_combos[digit]=seq
#                        print(f"[COMBO] {digit} = {seq}")
#                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                continue
#            if stripped.startswith("/crun "):
#                d=stripped.split(None,1)[1].strip(); run_global_combo(d); continue
#            if stripped.startswith("/cclear "):
#                d=stripped.split(None,1)[1].strip()
#                if d in global_combos:
#                    del global_combos[d]; print(f"[COMBO] Cleared {d}")
#                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                else:
#                    print(f"[COMBO] {d} not defined")
#                continue
#            if stripped=="/csave": save_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/cload": load_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/crun_all": run_all_global_combos(); continue
#
#            # General
#            if stripped=="/quit": print("[INFO] Quit"); break
#            if stripped=="/slots": show_slots(); continue
#
#            # Slot definitions
#            if stripped.startswith("/enter") and len(stripped)==7:
#                k=stripped[6].lower()
#                if k in slot_cmds:
#                    slot_cmds[k]={"type":"enter"}
#                    print(f"[SET] Slot {k} = <ENTER>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/combo") and len(stripped)>=7:
#                k=stripped[6].lower()
#                if k in slot_cmds:
#                    parts=line.split(None,1); seq=""
#                    if len(parts)>1:
#                        seq="".join(ch for ch in parts[1] if ch.isalnum())
#                    slot_cmds[k]={"type":"combo","seq":seq}
#                    print(f"[SET] Slot {k} = <COMBO {seq}>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/set") and len(stripped)>=5:
#                k=stripped[4].lower()
#                if k in slot_cmds:
#                    parts=line.split(None,1); data=parts[1] if len(parts)>1 else ""
#                    slot_cmds[k]={"type":"raw","data":data}
#                    print(f"[SET] Slot {k} raw length={len(data)}")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/clr") and len(stripped)==5:
#                k=stripped[4].lower()
#                if k in slot_cmds:
#                    slot_cmds[k]=None
#                    print(f"[CLR] Slot {k} cleared")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            # Play slot
#            if len(stripped)==2 and stripped[0] in ('o','O'):
#                k=stripped[1].lower()
#                if k in slot_cmds: play_slot(k)
#                continue
#
#            # Blank line
#            if line=="":
#                send_enter_only(safe=False); continue
#
#            # Normal input
#            try: body=line.encode(ENCODING,errors="replace")
#            except Exception as e:
#                print(f"[WARN] Encode failed: {e}")
#                continue
#            send_bytes(body+line_suffix(), safe=False, tag="TX")
#
#    except KeyboardInterrupt:
#        print("\n[INFO] KeyboardInterrupt")
#    finally:
#        persist_user()
#        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#        if 'hotkey_thread' in locals() and hotkey_thread:
#            stop_hotkey.set(); hotkey_thread.join(timeout=0.5)
#        reader.stop()
#        time.sleep(0.05)
#        try: ser.close()
#        except: pass
#        if 'log_file' in locals() and log_file:
#            try: log_file.close()
#            except: pass
#        print("[INFO] Exit")
#
#if __name__ == "__main__":
#    main()
#    
#
#
#
#    """


















    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
250904_0001_i2cdump_data_compare_pass

    """



#    """
##!/usr/bin/env python
## -*- coding: utf-8 -*-
#"""
#Serial console with:
# - Slots 0-9 + a-z (36 total)
# - Digit global combos (0-9)
# - Hotkeys: Ctrl+0..9 / Ctrl+a..z (play slot), Ctrl+S (show all), C+B+<digit> (single combo), C+L (list combos)
# - i2cdump capture & storage (/dumpsave /dumpshow /dumplist /dumpcmp)
# - Tolerant i2cdump capture (header or first data row, prompt line, overflow guard)
# - /dumpcmp now prints two blocks:
#       disk:<a>
#         header
#         row lines with unchanged bytes = XX, changed bytes = original value from dump A
#       disk:<b>
#         header
#         row lines with unchanged bytes = XX, changed bytes = original value from dump B
#   (If dumps are identical all bytes show as XX, matching the requested format.)
#
#Receiver thread style preserved (only feed hook).
#"""
#
#import sys
#import serial
#import threading
#import time
#import os
#import json
#import re
#from datetime import datetime
#
#try:
#    from serial.tools import list_ports
#except ImportError:
#    list_ports = None
#
## ================== Config (overridden by saved user config) ==================
#PORT                    = "COM5"
#BAUD                    = 115200
#PARITY_NAME             = "none"
#DATA_BITS               = 8
#STOP_BITS               = 1
#FLOW_CTRL               = "none"
#ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
#ENCODING                = "utf-8"
#TIMEOUT                 = 0.05
#CHAR_DELAY_MS           = 0
#LINE_DELAY_MS           = 0
#ASSERT_DTR              = False
#ASSERT_RTS              = False
#CLEAR_BUFF_ON_OPEN      = False
#
#TX_HEX                  = True
#HEX_DUMP_RX             = False
#RAW_RX                  = False
#QUIET_RX                = False
#
#LOG_PATH                = None
#INI_PATH                = None
#NO_BANNER               = False
#
#INTERACTIVE_SELECT      = True
#REMEMBER_LAST           = True
#LAST_FILE_NAME          = ".last_port"
#
#SLOTS_SAVE_FILE         = ".slot_cmds.json"
#AUTO_SAVE_SLOTS         = True
#SHOW_SAVE_MESSAGE       = True
#
#COMBO_SAVE_FILE         = ".combo_defs.json"
#AUTO_SAVE_COMBOS        = True
#SHOW_COMBO_SAVE_MSG     = True
#
#USER_CONFIG_FILE        = ".console_config.json"
#AUTO_SAVE_CONFIG        = True
#
#I2C_DUMP_SAVE_FILE      = ".i2c_dumps.json"
#AUTO_SAVE_I2C_DUMPS     = True
#MAX_I2C_DUMPS           = 10   # 0-9
#
#SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
#SCRIPT_LOCAL_ECHO         = False
#
#PROMPT_PATTERN            = "i2c>"
#SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
#SCRIPT_WAIT_PROMPT        = True
#POST_PROMPT_STABILIZE_MS  = 5
#
#HOTKEY_POLL_INTERVAL_SEC  = 0.05
#TOKEN_ENTER               = "<ENTER>"
#
#DIGIT_SLOTS  = [str(i) for i in range(10)]
#LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
#ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS
#
## ======================================================================
## Utility
## ======================================================================
#def format_hex(data: bytes) -> str:
#    return " ".join(f"{b:02X}" for b in data)
#
#def parse_ini(path: str):
#    out = {}
#    if not path or not os.path.isfile(path):
#        return out
#    try:
#        with open(path, "r", encoding="utf-8", errors="ignore") as f:
#            for line in f:
#                line=line.strip()
#                if not line or line.startswith(";") or "=" not in line:
#                    continue
#                k,v=line.split("=",1)
#                k=k.strip(); v=v.strip()
#                kl=k.lower()
#                if kl in ("comport","baudrate","delayperchar","delayperline"):
#                    try: out[k]=int(v)
#                    except: pass
#                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
#                    out[k]=v
#    except Exception as e:
#        print(f"[WARN] INI parse failed: {e}")
#    return out
#
#def load_user_config():
#    if not os.path.isfile(USER_CONFIG_FILE):
#        return {}
#    try:
#        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        return data if isinstance(data,dict) else {}
#    except Exception as e:
#        print(f"[CFG] Load failed: {e}")
#        return {}
#
#def save_user_config(cfg):
#    if not AUTO_SAVE_CONFIG: return
#    try:
#        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
#            json.dump(cfg,f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[CFG] Save failed: {e}")
#
#def normalize_slot_value(v):
#    if v is None: return None
#    if isinstance(v,dict):
#        t=v.get("type")
#        if t=="raw":
#            d=v.get("data"); return {"type":"raw","data": d if isinstance(d,str) else ""}
#        if t=="enter": return {"type":"enter"}
#        if t=="combo":
#            seq=v.get("seq","")
#            if not isinstance(seq,str): seq=""
#            return {"type":"combo","seq":seq}
#        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
#    if isinstance(v,str): return {"type":"raw","data":v}
#    return {"type":"raw","data":str(v)}
#
#def load_slots_from_file(path, slot_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        changed=False
#        for k in slot_dict.keys():
#            if k in data:
#                slot_dict[k]=normalize_slot_value(data[k]); changed=True
#        if changed: print(f"[SLOTS] Loaded {path}")
#    except Exception as e:
#        print(f"[SLOTS] Load failed: {e}")
#
#def save_slots_to_file(path, slot_dict):
#    try:
#        out={k:(None if v is None else v) for k,v in slot_dict.items()}
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(out,f,ensure_ascii=False,indent=2)
#        if SHOW_SAVE_MESSAGE: print(f"[SLOTS] Saved -> {path}")
#    except Exception as e:
#        print(f"[SLOTS] Save failed: {e}")
#
#def load_global_combos(path, combo_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
#                    combo_dict[k]="".join(ch for ch in v if ch.isalnum())
#            print(f"[COMBO] Loaded {path} ({len(combo_dict)} items)")
#    except Exception as e:
#        print(f"[COMBO] Load failed: {e}")
#
#def save_global_combos(path, combo_dict):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
#        if SHOW_COMBO_SAVE_MSG: print(f"[COMBO] Saved -> {path}")
#    except Exception as e:
#        print(f"[COMBO] Save failed: {e}")
#
#def load_i2c_dumps(path, dump_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if k in dump_dict and isinstance(v,list):
#                    dump_dict[k]=v
#        print(f"[DUMPS] Loaded {path}")
#    except Exception as e:
#        print(f"[DUMPS] Load failed: {e}")
#
#def save_i2c_dumps(path, dump_dict):
#    if not AUTO_SAVE_I2C_DUMPS: return
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(dump_dict,f,ensure_ascii=False,indent=2)
#        print(f"[DUMPS] Saved -> {path}")
#    except Exception as e:
#        print(f"[DUMPS] Save failed: {e}")
#
## ======================================================================
## Prompt tracking
## ======================================================================
#prompt_lock=threading.Lock()
#prompt_seq=0
#def inc_prompt_if_in(text:str):
#    global prompt_seq
#    if PROMPT_PATTERN and PROMPT_PATTERN in text:
#        with prompt_lock:
#            prompt_seq+=1
#def get_prompt_seq():
#    with prompt_lock:
#        return prompt_seq
#def wait_for_next_prompt(prev_seq, timeout):
#    if not SCRIPT_WAIT_PROMPT: return prev_seq
#    deadline=time.time()+timeout
#    while time.time()<deadline:
#        cur=get_prompt_seq()
#        if cur>prev_seq:
#            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
#            return cur
#        time.sleep(0.01)
#    return get_prompt_seq()
#
## ======================================================================
## i2cdump capture logic
## ======================================================================
#_i2c_capture_buffer_fragment=""
#_i2c_capture_active=False
#_i2c_capture_lines=[]
#_last_captured_dump=None
#
#_I2C_HEADER_RE = re.compile(r'^\s+00(?:\s+[0-9A-Fa-f]{2}){15}\s*$')
#_I2C_DATA_ROW_RE = re.compile(r'^[0-9A-Fa-f]{2}:\s+([0-9A-Fa-f]{2}\s+){0,15}[0-9A-Fa-f]{2}\s*$')
#_LAST_ADDR = "f0"
#
#def _maybe_finalize_partial(reason:str):
#    global _i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if _i2c_capture_active and _i2c_capture_lines:
#        _last_captured_dump=_i2c_capture_lines[:]
#        print(f"\n[DUMPS] Captured ({reason}) {len(_last_captured_dump)} lines")
#    _i2c_capture_active=False
#    _i2c_capture_lines=[]
#
#def _i2c_capture_feed(chunk:str):
#    global _i2c_capture_buffer_fragment,_i2c_capture_active,_i2c_capture_lines,_last_captured_dump
#    if not chunk: return
#    _i2c_capture_buffer_fragment += chunk
#    while True:
#        if '\n' not in _i2c_capture_buffer_fragment:
#            break
#        line,rest=_i2c_capture_buffer_fragment.split('\n',1)
#        _i2c_capture_buffer_fragment=rest
#        line=line.rstrip('\r')
#        if PROMPT_PATTERN and line.startswith(PROMPT_PATTERN):
#            if _i2c_capture_active:
#                _maybe_finalize_partial("prompt")
#            continue
#        if not _i2c_capture_active:
#            if _I2C_HEADER_RE.match(line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=[line]
#                continue
#            if re.match(r'^00:\s', line):
#                _i2c_capture_active=True
#                _i2c_capture_lines=["#NO_HEADER#"]
#            else:
#                continue
#        if _i2c_capture_active:
#            if _I2C_DATA_ROW_RE.match(line) or line==_i2c_capture_lines[0]:
#                if line != _i2c_capture_lines[0]:
#                    _i2c_capture_lines.append(line)
#            else:
#                if line.strip():
#                    _i2c_capture_lines.append(line)
#            if line.lower().startswith(_LAST_ADDR + ":"):
#                _last_captured_dump=_i2c_capture_lines[:]
#                print(f"\n[DUMPS] Captured i2cdump ({len(_last_captured_dump)} lines)")
#                _i2c_capture_active=False
#                _i2c_capture_lines=[]
#                continue
#            if len(_i2c_capture_lines) > 60:
#                _maybe_finalize_partial("overflow")
#                continue
#
## ======================================================================
## Receiver thread
## ======================================================================
#class SerialReaderThread(threading.Thread):
#    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
#        super().__init__(daemon=True)
#        self.ser=ser; self.encoding=encoding
#        self.hex_dump=hex_dump; self.raw=raw
#        self.log_file=log_file; self.quiet=quiet
#        self._running=True
#    def stop(self): self._running=False
#    def run(self):
#        while self._running and self.ser.is_open:
#            try:
#                data=self.ser.read(self.ser.in_waiting or 1)
#            except serial.SerialException as e:
#                print(f"[ERR] Serial exception: {e}")
#                break
#            if not data: continue
#            if self.log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try:
#                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n"); self.log_file.flush()
#                except Exception: pass
#            if self.quiet: continue
#            if self.hex_dump:
#                txt=format_hex(data)
#                print(f"[RX HEX] {txt}")
#                inc_prompt_if_in(txt)
#                _i2c_capture_feed(txt+"\n")
#            elif self.raw:
#                sys.stdout.buffer.write(data); sys.stdout.flush()
#                try:
#                    decoded=data.decode(self.encoding,errors="ignore")
#                    inc_prompt_if_in(decoded)
#                    _i2c_capture_feed(decoded)
#                except: pass
#            else:
#                try:
#                    text=data.decode(self.encoding,errors="replace")
#                except Exception:
#                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
#                print(text,end="",flush=True)
#                inc_prompt_if_in(text)
#                _i2c_capture_feed(text)
#
## ======================================================================
## Port selection
## ======================================================================
#def load_last_port():
#    if not REMEMBER_LAST: return None
#    try:
#        if os.path.isfile(LAST_FILE_NAME):
#            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
#                v=f.read().strip()
#                if v: return v
#    except: pass
#    return None
#def save_last_port(p):
#    if not REMEMBER_LAST: return
#    try:
#        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
#            f.write(p.strip())
#    except: pass
#
#def interactive_select_port(default_port):
#    port=default_port; baud=BAUD; parity_name=PARITY_NAME
#    data_bits=DATA_BITS; stop_bits=STOP_BITS; flow_ctrl=FLOW_CTRL; enter_mode=ENTER_MODE
#    last=load_last_port()
#    if last: default_port=last
#    if not INTERACTIVE_SELECT:
#        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#    print("=== Serial Interactive Config (Enter to keep default) ===")
#    if list_ports:
#        ports=list(list_ports.comports())
#        if ports:
#            print("Available ports:")
#            for idx,p in enumerate(ports,1):
#                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
#        else:
#            print("No COM ports detected.")
#    val=input(f"Port [{default_port}]: ").strip()
#    if val: port=val
#    val=input(f"Baud [{baud}]: ").strip()
#    if val.isdigit(): baud=int(val)
#    plist=["none","even","odd","mark","space"]
#    val=input(f"Parity {plist} [{parity_name}]: ").strip().lower()
#    if val in plist: parity_name=val
#    val=input(f"Data bits (7/8) [{data_bits}]: ").strip()
#    if val in ("7","8"): data_bits=int(val
#)
#    val=input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
#    if val in ("1","2"): stop_bits=int(val)
#    flist=["none","rtscts","dsrdtr","x"]
#    val=input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
#    if val in flist: flow_ctrl=val
#    emlist=["CR","CRLF","LF","NONE"]
#    val=input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
#    if val in emlist: enter_mode=val
#    save_last_port(port)
#    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#
## ======================================================================
## Hotkey Thread
## ======================================================================
#class HotkeyThread(threading.Thread):
#    def __init__(self,
#                 play_callback,
#                 show_all_callback,
#                 combo_list_callback,
#                 run_single_combo_callback,
#                 stop_event):
#        super().__init__(daemon=True)
#        self.play_callback=play_callback
#        self.show_all_callback=show_all_callback
#        self.combo_list_callback=combo_list_callback
#        self.run_single_combo_callback=run_single_combo_callback
#        self.stop_event=stop_event
#        import ctypes
#        self.ctypes=ctypes
#        self.user32=ctypes.WinDLL("user32", use_last_error=True)
#        self.VK_CTRL=0x11; self.VK_S=0x53
#        self.VK_C=0x43; self.VK_B=0x42; self.VK_L=0x4C
#        self.VK_0_9=list(range(0x30,0x3A))
#        self.VK_NUM_0_9=list(range(0x60,0x6A))
#        self.VK_A_Z=list(range(0x41,0x5B))
#        self.prev_digit_down={vk:False for vk in self.VK_0_9+self.VK_NUM_0_9}
#        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
#        self.prev_s_down=False
#        self.prev_cb=False
#        self.prev_cl_combo_list=False
#    def key_down(self,vk):
#        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
#    def run(self):
#        while not self.stop_event.is_set():
#            ctrl=self.key_down(self.VK_CTRL)
#            s_now=ctrl and self.key_down(self.VK_S)
#            if s_now and not self.prev_s_down:
#                print(); self.show_all_callback()
#            self.prev_s_down=s_now
#            c_now=self.key_down(self.VK_C); b_now=self.key_down(self.VK_B)
#            cb_now=c_now and b_now
#            if cb_now:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        self.run_single_combo_callback(key)
#                    self.prev_digit_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#            self.prev_cb=cb_now
#            l_now=self.key_down(self.VK_L); cl_now=c_now and l_now
#            if cl_now and not self.prev_cl_combo_list:
#                print(); self.combo_list_callback()
#            self.prev_cl_combo_list=cl_now
#            if ctrl:
#                for vk in self.VK_0_9+self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key=chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        print(); self.play_callback(key.lower())
#                    self.prev_digit_down[vk]=now
#                for vk in self.VK_A_Z:
#                    now=self.key_down(vk)
#                    if now and not self.prev_letter_down[vk]:
#                        key=chr(vk).lower()
#                        print(); self.play_callback(key)
#                    self.prev_letter_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
#                self.prev_s_down=False
#            time.sleep(HOTKEY_POLL_INTERVAL_SEC)
#
## ======================================================================
## Main
## ======================================================================
#def main():
#    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO, _last_captured_dump
#    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
#    user_cfg=load_user_config()
#
#    if "char_delay_ms" in user_cfg:
#        try: globals()['CHAR_DELAY_MS']=float(user_cfg["char_delay_ms"])
#        except: pass
#    if "line_delay_ms" in user_cfg:
#        try: globals()['LINE_DELAY_MS']=float(user_cfg["line_delay_ms"])
#        except: pass
#    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
#    if "script_char_delay_ms" in user_cfg:
#        try:
#            v=float(user_cfg["script_char_delay_ms"])
#            if v>=0: SAFE_SCRIPT_CHAR_DELAY_MS=v
#        except: pass
#    if "script_local_echo" in user_cfg:
#        SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
#
#    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
#    init_baud=cfg_ini.get("BaudRate",BAUD)
#    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
#    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
#    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
#    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
#    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
#    if init_enter not in ("CR","CRLF","LF","NONE"): init_enter="CR"
#
#    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)
#
#    parity_map={"even":serial.PARITY_EVEN,"odd":serial.PARITY_ODD,"none":serial.PARITY_NONE,
#                "mark":serial.PARITY_MARK,"space":serial.PARITY_SPACE}
#    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
#    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
#    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE
#
#    if fc in ("rtscts","hard"):
#        rtscts,dsrdtr,xonxoff=True,False,False
#    elif fc=="dsrdtr":
#        rtscts,dsrdtr,xonxoff=False,True,False
#    elif fc=="x":
#        rtscts,dsrdtr,xonxoff=False,False,True
#    else:
#        rtscts=dsrdtr=xonxoff=False
#
#    char_delay=float(globals()['CHAR_DELAY_MS']); line_delay=float(globals()['LINE_DELAY_MS'])
#
#    try:
#        ser=serial.Serial(port,baud,timeout=TIMEOUT,
#                          bytesize=bytesize,parity=parity,stopbits=stopbits,
#                          rtscts=rtscts,dsrdtr=dsrdtr,xonxoff=xonxoff,write_timeout=1)
#    except serial.SerialException as e:
#        print(f"[ERR] Cannot open {port}: {e}"); return
#
#    try:
#        if ASSERT_DTR: ser.setDTR(True)
#        if ASSERT_RTS: ser.setRTS(True)
#    except Exception as e:
#        print(f"[WARN] Setting DTR/RTS failed: {e}")
#
#    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
#        try: ser.reset_input_buffer(); ser.reset_output_buffer()
#        except Exception as e: print(f"[WARN] Clear buffers failed: {e}")
#
#    if not NO_BANNER:
#        print(f"[INFO] Opened {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
#        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
#        print(f"[INFO] Enter={enter_mode} char_delay={char_delay}ms line_delay={line_delay}ms script_min={SAFE_SCRIPT_CHAR_DELAY_MS}ms hex={'ON' if TX_HEX else 'OFF'} echo={'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#        print("[INFO] Type /help for command list.")
#
#    log_file=None
#    if LOG_PATH:
#        try:
#            log_file=open(LOG_PATH,"a",encoding="utf-8")
#            print(f"[INFO] Logging to {LOG_PATH}")
#        except Exception as e:
#            print(f"[WARN] Log open failed: {e}")
#
#    reader=SerialReaderThread(
#        ser,encoding=ENCODING,hex_dump=HEX_DUMP_RX,raw=RAW_RX,
#        log_file=log_file,quiet=QUIET_RX
#    )
#    reader.start()
#
#    send_lock=threading.Lock()
#
#    def persist_user():
#        user_cfg["char_delay_ms"]=char_delay
#        user_cfg["line_delay_ms"]=line_delay
#        user_cfg["tx_hex"]=TX_HEX
#        user_cfg["script_char_delay_ms"]=SAFE_SCRIPT_CHAR_DELAY_MS
#        user_cfg["script_local_echo"]=SCRIPT_LOCAL_ECHO
#        save_user_config(user_cfg)
#
#    def line_suffix():
#        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]
#
#    def send_bytes(data:bytes, tag="TX", safe=False, local_echo_line=None):
#        if not data: return
#        per_char_delay=char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
#        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
#            print(local_echo_line)
#        if per_char_delay>0 and len(data)>1:
#            for i,b in enumerate(data):
#                with send_lock:
#                    try: ser.write(bytes([b])); ser.flush()
#                    except serial.SerialException as e:
#                        print(f"[ERR] TX failed: {e}"); return
#                if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(bytes([b]))}")
#                if log_file:
#                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
#                    except: pass
#                if i<len(data)-1: time.sleep(per_char_delay/1000.0)
#        else:
#            with send_lock:
#                try: ser.write(data); ser.flush()
#                except serial.SerialException as e:
#                    print(f"[ERR] TX failed: {e}"); return
#            if TX_HEX and not QUIET_RX: print(f"[{tag} HEX] {format_hex(data)}")
#            if log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
#                except: pass
#        if line_delay>0 and tag.startswith("TX"): time.sleep(line_delay/1000.0)
#
#    class ScriptContext:
#        def __init__(self):
#            self.last_prompt_seq=get_prompt_seq(); self.first_send=True
#        def wait_ready_if_needed(self):
#            if not SCRIPT_WAIT_PROMPT: return
#            if self.first_send:
#                self.first_send=False; return
#            prev=self.last_prompt_seq
#            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
#        def note_after_send(self): pass
#
#    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        try: body=text.encode(ENCODING,errors="replace")
#        except Exception as e: print(f"[WARN] Encode failed: {e}"); return
#        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    def send_enter_only(safe=False, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        send_bytes(line_suffix(), tag="TX-EMPTY", safe=safe)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    slot_cmds={k:None for k in ALL_SLOTS}; load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
#    global_combos={}; load_global_combos(COMBO_SAVE_FILE, global_combos)
#
#    i2c_dump_slots={str(i):None for i in range(MAX_I2C_DUMPS)}
#    load_i2c_dumps(I2C_DUMP_SAVE_FILE, i2c_dump_slots)
#
#    def show_slots():
#        print("[SLOTS] ---------------------------")
#        for k in DIGIT_SLOTS+LETTER_SLOTS:
#            v=slot_cmds.get(k)
#            if v is None: print(f" {k}: (empty)")
#            else:
#                t=v.get("type")
#                if t=="enter": print(f" {k}: <ENTER>")
#                elif t=="combo": print(f" {k}: <COMBO {v.get('seq','')}>")
#                else:
#                    data=v.get("data",""); first=data.splitlines()[0] if data else ""
#                    more=" ..." if "\n" in data else ""
#                    print(f" {k}: {first[:60]}{more}")
#        print("[SLOTS] ---------------------------")
#
#    def show_global_combos():
#        print("[COMBOS] (digits 0-9) -------------")
#        if not global_combos: print(" (none)")
#        else:
#            for d in DIGIT_SLOTS:
#                if d in global_combos: print(f" {d}: {global_combos[d]}")
#                else: print(f" {d}: (empty)")
#        print("[COMBOS] ---------------------------")
#
#    def dumplist():
#        print("[DUMPS] 0-9 stored snapshots -------")
#        for d in DIGIT_SLOTS:
#            v=i2c_dump_slots.get(d)
#            print(f" {d}: {(str(len(v))+' lines') if v else '(empty)'}")
#        print("[DUMPS] ---------------------------")
#
#    def dump_show(d):
#        v=i2c_dump_slots.get(d)
#        if not v: print(f"[DUMPS] Slot {d} empty"); return
#        print(f"[DUMPS] Slot {d} ({len(v)} lines)")
#        for line in v: print(line)
#
#    # --- Updated compare (diff with XX for unchanged bytes) ---
#    HEADER_LINE = "     " + " ".join(f"{i:02x}" for i in range(16))
#    ROW_ADDRS = [f"{i:02x}" for i in range(0,256,16)]
#
#    def _parse_dump_to_matrix(lines):
#        # returns dict addr_row -> list of 16 byte strings (uppercase), missing filled with '--'
#        matrix={}
#        for ln in lines:
#            if ln.startswith("#NO_HEADER#"):
#                continue
#            m=re.match(r'^([0-9A-Fa-f]{2}):\s+(.*)$', ln)
#            if not m: continue
#            addr=m.group(1).lower()
#            rest=m.group(2).strip()
#            bytes_list=[b for b in rest.split() if re.fullmatch(r'[0-9A-Fa-f]{2}', b)]
#            if len(bytes_list)<16:
#                bytes_list += ["--"]*(16-len(bytes_list))
#            elif len(bytes_list)>16:
#                bytes_list=bytes_list[:16]
#            matrix[addr]=[b.upper() for b in bytes_list]
#        # fill any missing rows with placeholder bytes
#        for a in ROW_ADDRS:
#            if a not in matrix:
#                matrix[a]=["--"]*16
#        return matrix
#
#    def dump_compare(a,b):
#        da=i2c_dump_slots.get(a)
#        db=i2c_dump_slots.get(b)
#        if not da:
#            print(f"[DUMPCMP] Slot {a} empty"); return
#        if not db:
#            print(f"[DUMPCMP] Slot {b} empty"); return
#        mA=_parse_dump_to_matrix(da)
#        mB=_parse_dump_to_matrix(db)
#        print(f"[DUMPCMP] diff view (unchanged=XX)  {a} vs {b}")
#        # disk a
#        print(f"disk:{a}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            out=[]
#            for i in range(16):
#                ba=rowA[i]; bb=rowB[i]
#                if ba==bb:
#                    out.append("XX")
#                else:
#                    out.append(ba)
#            print(f"{addr}:  {' '.join(out)}")
#        # disk b
#        print(f"disk:{b}")
#        print(HEADER_LINE)
#        for addr in ROW_ADDRS:
#            rowA=mA[addr]; rowB=mB[addr]
#            out=[]
#            for i in range(16):
#                ba=rowA[i]; bb=rowB[i]
#                if ba==bb:
#                    out.append("XX")
#                else:
#                    out.append(bb)
#            print(f"{addr}:  {' '.join(out)}")
#        print("[DUMPCMP] End")
#
#    def show_all():
#        show_slots(); show_global_combos(); dumplist()
#
#    def print_help():
#        print("""[HELP]
#Slots (0-9,a-z):
#  /setX <text>   /comboX <seq>  /enterX  /clrX  oX  /slots  /slotsave  /slotload
#Global combos (0-9):
#  /cset d <seq>  /clist  /crun d  /cclear d  /crun_all  /csave  /cload
#i2cdump capture:
#  (Run i2cdump normally)
#  /dumpsave d    /dumpshow d    /dumplist
#  /dumpcmp a b   (prints two blocks; unchanged bytes shown as XX)
#Delays & modes:
#  /delay /scriptdelay /linedelay /hex on|off /scriptecho on|off
#General:
#  /help /quit
#Hotkeys (Win):
#  Ctrl+0..9 / Ctrl+a..z play slot
#  Ctrl+S show slots+combos+dumps
#  C+B+digit run digit combo
#  C+L list digit combos
#""")
#
#    def play_slot_recursive(idx_char, depth, visited, script_ctx):
#        if depth>40: print("[PLAY] Depth limit"); return
#        if idx_char not in slot_cmds: print(f"[PLAY] Slot {idx_char} not found"); return
#        v=slot_cmds[idx_char]
#        if v is None: print(f"[PLAY] Slot {idx_char} empty"); return
#        if id(v) in visited: print(f"[PLAY] Cycle at {idx_char}"); return
#        visited.add(id(v))
#        t=v.get("type")
#        if t=="enter":
#            send_enter_only(safe=True, script_ctx=script_ctx)
#        elif t=="combo":
#            for c in v.get("seq",""):
#                if c in slot_cmds: play_slot_recursive(c, depth+1, visited, script_ctx)
#        else:
#            data=v.get("data","")
#            parts=data.split(TOKEN_ENTER)
#            for pi,segment in enumerate(parts):
#                lines=segment.splitlines()
#                if not lines and segment=="": send_enter_only(safe=True, script_ctx=script_ctx)
#                for line in lines:
#                    if line.strip()=="" and line!="":
#                        send_enter_only(safe=True, script_ctx=script_ctx)
#                    elif line!="":
#                        send_line(line,safe=True,
#                                  local_echo=f"[RUN] {line}" if SCRIPT_LOCAL_ECHO else None,
#                                  script_ctx=script_ctx)
#                if pi<len(parts)-1: send_enter_only(safe=True, script_ctx=script_ctx)
#        visited.remove(id(v))
#
#    def play_slot(k):
#        if k not in slot_cmds: print(f"[PLAY] Slot {k} invalid"); return
#        print(f"[PLAY] Slot {k}")
#        ctx=ScriptContext()
#        play_slot_recursive(k,0,set(),ctx)
#
#    def run_global_combo(d):
#        if d not in global_combos: print(f"[COMBO] Digit {d} undefined"); return
#        seq=global_combos[d]; print(f"[COMBO] Run {d}: {seq}")
#        ctx=ScriptContext()
#        for c in seq:
#            if c in slot_cmds: play_slot_recursive(c,0,set(),ctx)
#
#    def run_all_global_combos():
#        defined=[d for d in DIGIT_SLOTS if d in global_combos]
#        if not defined: print("[COMBO] No digit combos defined"); return
#        print("[COMBO] Run ALL digit combos:")
#        ctx=ScriptContext()
#        for d in defined:
#            seq=global_combos[d]; print(f"  -> {d}: {seq}")
#            for c in seq:
#                if c in slot_cmds: play_slot_recursive(c,0,set(),ctx)
#
#    def run_single_combo_via_hotkey(d):
#        if d in global_combos:
#            print(f"[COMBO] (Hotkey) {d}")
#            run_global_combo(d)
#        else:
#            print(f"[COMBO] (Hotkey) {d} undefined")
#
#    stop_hotkey=threading.Event()
#    hotkey_thread=None
#    if os.name=='nt':
#        try:
#            hotkey_thread=HotkeyThread(
#                play_callback=play_slot,
#                show_all_callback=show_all,
#                combo_list_callback=show_global_combos,
#                run_single_combo_callback=run_single_combo_via_hotkey,
#                stop_event=stop_hotkey
#            ); hotkey_thread.start()
#        except Exception as e:
#            print(f"[WARN] Hotkey thread failed: {e}")
#
#    # Command loop
#    try:
#        while True:
#            try: line=input()
#            except EOFError: break
#            stripped=line.strip()
#
#            if stripped=="/help": print_help(); continue
#
#            # i2cdump commands
#            if stripped.startswith("/dumpsave"):
#                parts=stripped.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[DUMPS] Usage: /dumpsave <digit>")
#                else:
#                    d=parts[1]
#                    if _last_captured_dump:
#                        i2c_dump_slots[d]=_last_captured_dump[:]
#                        print(f"[DUMPS] Saved capture to slot {d} ({len(_last_captured_dump)} lines)")
#                        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#                    else:
#                        print("[DUMPS] No captured dump to save")
#                continue
#            if stripped.startswith("/dumpshow"):
#                parts=stripped.split()
#                if len(parts)!=2 or parts[1] not in DIGIT_SLOTS:
#                    print("[DUMPS] Usage: /dumpshow <digit>")
#                else: dump_show(parts[1])
#                continue
#            if stripped.startswith("/dumpcmp"):
#                parts=stripped.split()
#                if len(parts)!=3 or parts[1] not in DIGIT_SLOTS or parts[2] not in DIGIT_SLOTS:
#                    print("[DUMPCMP] Usage: /dumpcmp <a> <b>")
#                else: dump_compare(parts[1],parts[2])
#                continue
#            if stripped=="/dumplist": dumplist(); continue
#
#            # Delays / modes
#            if stripped.startswith("/delay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[DELAY] {char_delay} ms")
#                else:
#                    try: v=float(parts[1]); assert v>=0; char_delay=v; print(f"[DELAY] -> {char_delay} ms"); persist_user()
#                    except: print(f"[DELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptdelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTDELAY] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
#                else:
#                    try: v=float(parts[1]); assert v>=0; SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[SCRIPTDELAY] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
#                    except: print(f"[SCRIPTDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/linedelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[LINEDELAY] {line_delay} ms")
#                else:
#                    try: v=float(parts[1]); assert v>=0; line_delay=v; print(f"[LINEDELAY] -> {line_delay} ms"); persist_user()
#                    except: print(f"[LINEDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptecho"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTECHO] {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        SCRIPT_LOCAL_ECHO=(arg=="on"); print(f"[SCRIPTECHO] -> {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}"); persist_user()
#                    else: print("[SCRIPTECHO] Use: /scriptecho on|off")
#                continue
#            if stripped.startswith("/hex"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[HEX] {'ON' if TX_HEX else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        TX_HEX=(arg=="on"); print(f"[HEX] -> {'ON' if TX_HEX else 'OFF'}"); persist_user()
#                    else: print("[HEX] Use: /hex on|off")
#                continue
#
#            # Slots persistence
#            if stripped=="/slotsave": save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
#            if stripped=="/slotload": load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue
#
#            # Combos
#            if stripped=="/clist": show_global_combos(); continue
#            if stripped.startswith("/cset "):
#                parts=stripped.split(None,2)
#                if len(parts)<3: print("[COMBO] Usage: /cset <digit> <seq>")
#                else:
#                    digit=parts[1]
#                    if not (digit.isdigit() and len(digit)==1):
#                        print("[COMBO] Name must be single digit (0-9)")
#                    else:
#                        seq="".join(ch for ch in parts[2] if ch.isalnum())
#                        global_combos[digit]=seq
#                        print(f"[COMBO] {digit} = {seq}")
#                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                continue
#            if stripped.startswith("/crun "):
#                d=stripped.split(None,1)[1].strip(); run_global_combo(d); continue
#            if stripped.startswith("/cclear "):
#                d=stripped.split(None,1)[1].strip()
#                if d in global_combos:
#                    del global_combos[d]; print(f"[COMBO] Cleared {d}")
#                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                else: print(f"[COMBO] {d} not defined")
#                continue
#            if stripped=="/csave": save_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/cload": load_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/crun_all": run_all_global_combos(); continue
#
#            # General
#            if stripped=="/quit": print("[INFO] Quit"); break
#            if stripped=="/slots": show_slots(); continue
#
#            # Slot defs
#            if stripped.startswith("/enter") and len(stripped)==7:
#                k=stripped[6].lower()
#                if k in slot_cmds:
#                    slot_cmds[k]={"type":"enter"}; print(f"[SET] Slot {k} = <ENTER>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/combo") and len(stripped)>=7:
#                k=stripped[6].lower()
#                if k in slot_cmds:
#                    parts=line.split(None,1); seq=""
#                    if len(parts)>1: seq="".join(ch for ch in parts[1] if ch.isalnum())
#                    slot_cmds[k]={"type":"combo","seq":seq}
#                    print(f"[SET] Slot {k} = <COMBO {seq}>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/set") and len(stripped)>=5:
#                k=stripped[4].lower()
#                if k in slot_cmds:
#                    parts=line.split(None,1); data=parts[1] if len(parts)>1 else ""
#                    slot_cmds[k]={"type":"raw","data":data}
#                    print(f"[SET] Slot {k} raw length={len(data)}")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#            if stripped.startswith("/clr") and len(stripped)==5:
#                k=stripped[4].lower()
#                if k in slot_cmds:
#                    slot_cmds[k]=None; print(f"[CLR] Slot {k} cleared")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            # Play slot
#            if len(stripped)==2 and stripped[0] in ('o','O'):
#                k=stripped[1].lower()
#                if k in slot_cmds: play_slot(k)
#                continue
#
#            # Blank line
#            if line=="": send_enter_only(safe=False); continue
#
#            # Normal input
#            try: body=line.encode(ENCODING,errors="replace")
#            except Exception as e: print(f"[WARN] Encode failed: {e}"); continue
#            send_bytes(body+line_suffix(), safe=False, tag="TX")
#
#    except KeyboardInterrupt:
#        print("\n[INFO] KeyboardInterrupt")
#    finally:
#        persist_user()
#        save_i2c_dumps(I2C_DUMP_SAVE_FILE,i2c_dump_slots)
#        if 'hotkey_thread' in locals() and hotkey_thread:
#            stop_hotkey.set(); hotkey_thread.join(timeout=0.5)
#        reader.stop()
#        time.sleep(0.05)
#        try: ser.close()
#        except: pass
#        if 'log_file' in locals() and log_file:
#            try: log_file.close()
#            except: pass
#        print("[INFO] Exit")
#
#if __name__ == "__main__":
#    main()
#    
#
#    """



















    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass
250903_0003_combo_basic_and_delay_pass
    """


##!/usr/bin/env python
## -*- coding: utf-8 -*-
#"""
#Serial console with:
# - Slots 0-9 + a-z (36 total) for stored commands (/setX /comboX /enterX /clrX, play with oX or hotkeys)
# - Global combos restricted to digits 0-9 only ( /cset <digit> <seq> )
# - Ctrl+0..9 / Ctrl+a..z plays corresponding slot
# - C + B + <digit> runs that single digit global combo (C+B alone now does NOTHING)
# - Ctrl+S shows BOTH slots and global combos (combined view)
# - Combo / slot sequence characters allowed: 0-9 a-z
# - Prompt wait safety for batch execution
# - Persistent settings (delays, hex flag, script echo)
# - Persistent slots and digit global combos
# - Receiver thread kept stylistically identical (no logic changes except unavoidable variable references)
#
#All user-facing messages and help are in English.
#"""
#
#import sys
#import serial
#import threading
#import time
#import os
#import json
#from datetime import datetime
#
#try:
#    from serial.tools import list_ports
#except ImportError:
#    list_ports = None
#
## ================== Config (overridden by saved user config) ==================
#PORT                    = "COM5"
#BAUD                    = 115200
#PARITY_NAME             = "none"
#DATA_BITS               = 8
#STOP_BITS               = 1
#FLOW_CTRL               = "none"
#ENTER_MODE              = "CR"      # CR / CRLF / LF / NONE
#ENCODING                = "utf-8"
#TIMEOUT                 = 0.05
#CHAR_DELAY_MS           = 0
#LINE_DELAY_MS           = 0
#ASSERT_DTR              = False
#ASSERT_RTS              = False
#CLEAR_BUFF_ON_OPEN      = False
#
#TX_HEX                  = True
#HEX_DUMP_RX             = False
#RAW_RX                  = False
#QUIET_RX                = False
#
#LOG_PATH                = None
#INI_PATH                = None
#NO_BANNER               = False
#
#INTERACTIVE_SELECT      = True
#REMEMBER_LAST           = True
#LAST_FILE_NAME          = ".last_port"
#
#SLOTS_SAVE_FILE         = ".slot_cmds.json"
#AUTO_SAVE_SLOTS         = True
#SHOW_SAVE_MESSAGE       = True
#
#COMBO_SAVE_FILE         = ".combo_defs.json"
#AUTO_SAVE_COMBOS        = True
#SHOW_COMBO_SAVE_MSG     = True
#
#USER_CONFIG_FILE        = ".console_config.json"
#AUTO_SAVE_CONFIG        = True
#
#SAFE_SCRIPT_CHAR_DELAY_MS = 1.0
#SCRIPT_LOCAL_ECHO         = False
#
#PROMPT_PATTERN            = "i2c>"
#SCRIPT_PROMPT_TIMEOUT_SEC = 5.0
#SCRIPT_WAIT_PROMPT        = True
#POST_PROMPT_STABILIZE_MS  = 5
#
#HOTKEY_POLL_INTERVAL_SEC  = 0.05
#TOKEN_ENTER               = "<ENTER>"
#
#DIGIT_SLOTS  = [str(i) for i in range(10)]
#LETTER_SLOTS = [chr(c) for c in range(ord('a'), ord('z') + 1)]
#ALL_SLOTS    = DIGIT_SLOTS + LETTER_SLOTS    # 0-9 + a-z
#
## ======================================================================
## Utility
## ======================================================================
#def format_hex(data: bytes) -> str:
#    return " ".join(f"{b:02X}" for b in data)
#
#def parse_ini(path: str):
#    out = {}
#    if not path or not os.path.isfile(path):
#        return out
#    try:
#        with open(path, "r", encoding="utf-8", errors="ignore") as f:
#            for line in f:
#                line = line.strip()
#                if not line or line.startswith(";") or "=" not in line:
#                    continue
#                k, v = line.split("=", 1)
#                k = k.strip(); v = v.strip()
#                kl = k.lower()
#                if kl in ("comport","baudrate","delayperchar","delayperline"):
#                    try: out[k] = int(v)
#                    except: pass
#                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
#                    out[k] = v
#    except Exception as e:
#        print(f"[WARN] INI parse failed: {e}")
#    return out
#
## ======================================================================
## User config persistence
## ======================================================================
#def load_user_config():
#    if not os.path.isfile(USER_CONFIG_FILE):
#        return {}
#    try:
#        with open(USER_CONFIG_FILE,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        return data if isinstance(data,dict) else {}
#    except Exception as e:
#        print(f"[CFG] Load failed: {e}")
#        return {}
#
#def save_user_config(cfg):
#    if not AUTO_SAVE_CONFIG: return
#    try:
#        with open(USER_CONFIG_FILE,"w",encoding="utf-8") as f:
#            json.dump(cfg,f,ensure_ascii=False,indent=2)
#    except Exception as e:
#        print(f"[CFG] Save failed: {e}")
#
## ======================================================================
## Slots persistence
## ======================================================================
#def normalize_slot_value(v):
#    if v is None: return None
#    if isinstance(v,dict):
#        t=v.get("type")
#        if t=="raw":
#            d=v.get("data")
#            return {"type":"raw","data": d if isinstance(d,str) else ""}
#        if t=="enter": return {"type":"enter"}
#        if t=="combo":
#            seq=v.get("seq","")
#            if not isinstance(seq,str): seq=""
#            return {"type":"combo","seq":seq}
#        return {"type":"raw","data":json.dumps(v,ensure_ascii=False)}
#    if isinstance(v,str):
#        return {"type":"raw","data":v}
#    return {"type":"raw","data":str(v)}
#
#def load_slots_from_file(path, slot_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        changed=False
#        for k in slot_dict.keys():
#            if k in data:
#                slot_dict[k]=normalize_slot_value(data[k])
#                changed=True
#        if changed:
#            print(f"[SLOTS] Loaded {path}")
#    except Exception as e:
#        print(f"[SLOTS] Load failed: {e}")
#
#def save_slots_to_file(path, slot_dict):
#    try:
#        out={}
#        for k,v in slot_dict.items():
#            out[k]=None if v is None else v
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(out,f,ensure_ascii=False,indent=2)
#        if SHOW_SAVE_MESSAGE:
#            print(f"[SLOTS] Saved -> {path}")
#    except Exception as e:
#        print(f"[SLOTS] Save failed: {e}")
#
## ======================================================================
## Global combos (digits only 0-9)
## ======================================================================
#def load_global_combos(path, combo_dict):
#    if not os.path.isfile(path): return
#    try:
#        with open(path,"r",encoding="utf-8") as f:
#            data=json.load(f)
#        if isinstance(data,dict):
#            for k,v in data.items():
#                if isinstance(k,str) and k.isdigit() and len(k)==1 and isinstance(v,str):
#                    combo_dict[k] = "".join(ch for ch in v if ch.isalnum())
#            print(f"[COMBO] Loaded {path} ({len(combo_dict)} items)")
#    except Exception as e:
#        print(f"[COMBO] Load failed: {e}")
#
#def save_global_combos(path, combo_dict):
#    try:
#        with open(path,"w",encoding="utf-8") as f:
#            json.dump(combo_dict,f,ensure_ascii=False,indent=2)
#        if SHOW_COMBO_SAVE_MSG:
#            print(f"[COMBO] Saved -> {path}")
#    except Exception as e:
#        print(f"[COMBO] Save failed: {e}")
#
## ======================================================================
## Prompt tracking
## ======================================================================
#prompt_lock = threading.Lock()
#prompt_seq  = 0
#def inc_prompt_if_in(text:str):
#    global prompt_seq
#    if PROMPT_PATTERN and PROMPT_PATTERN in text:
#        with prompt_lock:
#            prompt_seq += 1
#def get_prompt_seq():
#    with prompt_lock:
#        return prompt_seq
#def wait_for_next_prompt(prev_seq, timeout):
#    if not SCRIPT_WAIT_PROMPT: return prev_seq
#    deadline=time.time()+timeout
#    while time.time()<deadline:
#        cur=get_prompt_seq()
#        if cur>prev_seq:
#            time.sleep(POST_PROMPT_STABILIZE_MS/1000.0)
#            return cur
#        time.sleep(0.01)
#    return get_prompt_seq()
#
## ======================================================================
## Receiver thread (unchanged logic style)
## ======================================================================
#class SerialReaderThread(threading.Thread):
#    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
#        super().__init__(daemon=True)
#        self.ser=ser
#        self.encoding=encoding
#        self.hex_dump=hex_dump
#        self.raw=raw
#        self.log_file=log_file
#        self.quiet=quiet
#        self._running=True
#    def stop(self): self._running=False
#    def run(self):
#        while self._running and self.ser.is_open:
#            try:
#                data=self.ser.read(self.ser.in_waiting or 1)
#            except serial.SerialException as e:
#                print(f"[ERR] Serial exception: {e}")
#                break
#            if not data:
#                continue
#            if self.log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try:
#                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
#                    self.log_file.flush()
#                except Exception:
#                    pass
#            if self.quiet:
#                continue
#            if self.hex_dump:
#                txt=format_hex(data)
#                print(f"[RX HEX] {txt}")
#                inc_prompt_if_in(txt)
#            elif self.raw:
#                sys.stdout.buffer.write(data)
#                sys.stdout.flush()
#                try:
#                    inc_prompt_if_in(data.decode(self.encoding,errors="ignore"))
#                except:
#                    pass
#            else:
#                try:
#                    text=data.decode(self.encoding,errors="replace")
#                except Exception:
#                    text="".join(chr(b) if 32<=b<127 else f"\\x{b:02X}" for b in data)
#                print(text,end="",flush=True)
#                inc_prompt_if_in(text)
#
## ======================================================================
## Port selection / state persistence
## ======================================================================
#def load_last_port():
#    if not REMEMBER_LAST: return None
#    try:
#        if os.path.isfile(LAST_FILE_NAME):
#            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
#                v=f.read().strip()
#                if v: return v
#    except: pass
#    return None
#def save_last_port(p):
#    if not REMEMBER_LAST: return
#    try:
#        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
#            f.write(p.strip())
#    except: pass
#
#def interactive_select_port(default_port):
#    port=default_port
#    baud=BAUD
#    parity_name=PARITY_NAME
#    data_bits=DATA_BITS
#    stop_bits=STOP_BITS
#    flow_ctrl=FLOW_CTRL
#    enter_mode=ENTER_MODE
#    last=load_last_port()
#    if last: default_port=last
#    if not INTERACTIVE_SELECT:
#        return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#    print("=== Serial Interactive Config (Enter to keep default) ===")
#    if list_ports:
#        ports=list(list_ports.comports())
#        if ports:
#            print("Available ports:")
#            for idx,p in enumerate(ports,1):
#                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
#        else:
#            print("No COM ports detected.")
#    val=input(f"Port [{default_port}]: ").strip()
#    if val: port=val
#    val=input(f"Baud [{baud}]: ").strip()
#    if val.isdigit(): baud=int(val)
#    plist=["none","even","odd","mark","space"]
#    val=input(f"Parity {plist} [{parity_name}]: ").strip().lower()
#    if val in plist: parity_name=val
#    val=input(f"Data bits (7/8) [{data_bits}]: ").strip()
#    if val in ("7","8"): data_bits=int(val)
#    val=input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
#    if val in ("1","2"): stop_bits=int(val)
#    flist=["none","rtscts","dsrdtr","x"]
#    val=input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
#    if val in flist: flow_ctrl=val
#    emlist=["CR","CRLF","LF","NONE"]
#    val=input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
#    if val in emlist: enter_mode=val
#    save_last_port(port)
#    return port,baud,parity_name,data_bits,stop_bits,flow_ctrl,enter_mode
#
## ======================================================================
## Hotkey Thread (C+B alone now does nothing; only C+B+digit triggers single combo)
## ======================================================================
#class HotkeyThread(threading.Thread):
#    def __init__(self,
#                 play_callback,
#                 show_all_callback,
#                 combo_list_callback,
#                 run_single_combo_callback,
#                 stop_event):
#        super().__init__(daemon=True)
#        self.play_callback=play_callback
#        self.show_all_callback=show_all_callback
#        self.combo_list_callback=combo_list_callback
#        self.run_single_combo_callback=run_single_combo_callback
#        self.stop_event=stop_event
#        import ctypes
#        self.ctypes=ctypes
#        self.user32=ctypes.WinDLL("user32", use_last_error=True)
#        self.VK_CTRL=0x11
#        self.VK_S=0x53
#        self.VK_C=0x43
#        self.VK_B=0x42
#        self.VK_L=0x4C
#        self.VK_0_9=list(range(0x30,0x3A))
#        self.VK_NUM_0_9=list(range(0x60,0x6A))
#        self.VK_A_Z=list(range(0x41,0x5B))
#        self.prev_digit_down={vk:False for vk in self.VK_0_9 + self.VK_NUM_0_9}
#        self.prev_letter_down={vk:False for vk in self.VK_A_Z}
#        self.prev_s_down=False
#        self.prev_cb=False
#        self.prev_cl_combo_list=False
#    def key_down(self,vk):
#        return (self.user32.GetAsyncKeyState(vk) & 0x8000)!=0
#    def run(self):
#        while not self.stop_event.is_set():
#            ctrl=self.key_down(self.VK_CTRL)
#            # Ctrl+S => show slots + combos
#            s_now=ctrl and self.key_down(self.VK_S)
#            if s_now and not self.prev_s_down:
#                print()
#                self.show_all_callback()
#            self.prev_s_down=s_now
#
#            # C + B + digit => single combo
#            c_now=self.key_down(self.VK_C)
#            b_now=self.key_down(self.VK_B)
#            cb_now=c_now and b_now
#            if cb_now:
#                for vk in self.VK_0_9 + self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key = chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        self.run_single_combo_callback(key)
#                    self.prev_digit_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#            self.prev_cb=cb_now
#
#            # C + L => list combos only
#            l_now=self.key_down(self.VK_L)
#            cl_now=c_now and l_now
#            if cl_now and not self.prev_cl_combo_list:
#                print()
#                self.combo_list_callback()
#            self.prev_cl_combo_list=cl_now
#
#            # Ctrl + slot (digits + letters)
#            if ctrl:
#                # digits
#                for vk in self.VK_0_9 + self.VK_NUM_0_9:
#                    now=self.key_down(vk)
#                    if now and not self.prev_digit_down[vk]:
#                        key = chr(vk) if 0x30<=vk<=0x39 else str(vk-0x60)
#                        print()
#                        self.play_callback(key.lower())
#                    self.prev_digit_down[vk]=now
#                # letters
#                for vk in self.VK_A_Z:
#                    now=self.key_down(vk)
#                    if now and not self.prev_letter_down[vk]:
#                        key=chr(vk).lower()
#                        print()
#                        self.play_callback(key)
#                    self.prev_letter_down[vk]=now
#            else:
#                for vk in self.prev_digit_down: self.prev_digit_down[vk]=False
#                for vk in self.prev_letter_down: self.prev_letter_down[vk]=False
#                self.prev_s_down=False
#            time.sleep(HOTKEY_POLL_INTERVAL_SEC)
#
## ======================================================================
## Main
## ======================================================================
#def main():
#    global TX_HEX, SAFE_SCRIPT_CHAR_DELAY_MS, SCRIPT_LOCAL_ECHO
#    cfg_ini=parse_ini(INI_PATH) if INI_PATH else {}
#    user_cfg=load_user_config()
#
#    if "char_delay_ms" in user_cfg:
#        try: globals()['CHAR_DELAY_MS']=float(user_cfg["char_delay_ms"])
#        except: pass
#    if "line_delay_ms" in user_cfg:
#        try: globals()['LINE_DELAY_MS']=float(user_cfg["line_delay_ms"])
#        except: pass
#    if "tx_hex" in user_cfg: TX_HEX=bool(user_cfg["tx_hex"])
#    if "script_char_delay_ms" in user_cfg:
#        try:
#            v=float(user_cfg["script_char_delay_ms"])
#            if v>=0: SAFE_SCRIPT_CHAR_DELAY_MS=v
#        except: pass
#    if "script_local_echo" in user_cfg:
#        SCRIPT_LOCAL_ECHO=bool(user_cfg["script_local_echo"])
#
#    init_port=f"COM{cfg_ini['ComPort']}" if "ComPort" in cfg_ini else PORT
#    init_baud=cfg_ini.get("BaudRate",BAUD)
#    init_parity=(cfg_ini.get("Parity",PARITY_NAME)).lower()
#    init_data_bits=cfg_ini.get("DataBit",DATA_BITS)
#    init_stop_bits=cfg_ini.get("StopBit",STOP_BITS)
#    init_flow=cfg_ini.get("FlowCtrl",FLOW_CTRL).lower()
#    init_enter=cfg_ini.get("CRSend",ENTER_MODE).upper()
#    if init_enter not in ("CR","CRLF","LF","NONE"):
#        init_enter="CR"
#
#    (port, baud, parity_name, data_bits, stop_bits_val, fc, enter_mode)=interactive_select_port(init_port)
#
#    parity_map={
#        "even": serial.PARITY_EVEN,
#        "odd": serial.PARITY_ODD,
#        "none": serial.PARITY_NONE,
#        "mark": serial.PARITY_MARK,
#        "space": serial.PARITY_SPACE
#    }
#    parity=parity_map.get(parity_name.lower(),serial.PARITY_NONE)
#    bytesize=serial.SEVENBITS if data_bits==7 else serial.EIGHTBITS
#    stopbits=serial.STOPBITS_TWO if stop_bits_val==2 else serial.STOPBITS_ONE
#
#    if fc in ("rtscts","hard"):
#        rtscts, dsrdtr, xonxoff = True, False, False
#    elif fc=="dsrdtr":
#        rtscts, dsrdtr, xonxoff = False, True, False
#    elif fc=="x":
#        rtscts, dsrdtr, xonxoff = False, False, True
#    else:
#        rtscts = dsrdtr = xonxoff = False
#
#    char_delay=float(globals()['CHAR_DELAY_MS'])
#    line_delay=float(globals()['LINE_DELAY_MS'])
#
#    try:
#        ser=serial.Serial(
#            port, baud, timeout=TIMEOUT,
#            bytesize=bytesize, parity=parity, stopbits=stopbits,
#            rtscts=rtscts, dsrdtr=dsrdtr, xonxoff=xonxoff,
#            write_timeout=1
#        )
#    except serial.SerialException as e:
#        print(f"[ERR] Cannot open {port}: {e}")
#        return
#
#    try:
#        if ASSERT_DTR: ser.setDTR(True)
#        if ASSERT_RTS: ser.setRTS(True)
#    except Exception as e:
#        print(f"[WARN] Setting DTR/RTS failed: {e}")
#
#    if cfg_ini.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN:
#        try:
#            ser.reset_input_buffer()
#            ser.reset_output_buffer()
#        except Exception as e:
#            print(f"[WARN] Clear buffers failed: {e}")
#
#    if not NO_BANNER:
#        print(f"[INFO] Opened {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
#        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
#        print(f"[INFO] Enter={enter_mode} char_delay={char_delay}ms line_delay={line_delay}ms script_min={SAFE_SCRIPT_CHAR_DELAY_MS}ms hex={'ON' if TX_HEX else 'OFF'} echo={'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#        print("[INFO] Type /help for command list.")
#
#    log_file=None
#    if LOG_PATH:
#        try:
#            log_file=open(LOG_PATH,"a",encoding="utf-8")
#            print(f"[INFO] Logging to {LOG_PATH}")
#        except Exception as e:
#            print(f"[WARN] Log open failed: {e}")
#
#    reader=SerialReaderThread(
#        ser,
#        encoding=ENCODING,
#        hex_dump=HEX_DUMP_RX,
#        raw=RAW_RX,
#        log_file=log_file,
#        quiet=QUIET_RX
#    )
#    reader.start()
#
#    send_lock=threading.Lock()
#
#    def persist_user():
#        user_cfg["char_delay_ms"]=char_delay
#        user_cfg["line_delay_ms"]=line_delay
#        user_cfg["tx_hex"]=TX_HEX
#        user_cfg["script_char_delay_ms"]=SAFE_SCRIPT_CHAR_DELAY_MS
#        user_cfg["script_local_echo"]=SCRIPT_LOCAL_ECHO
#        save_user_config(user_cfg)
#
#    def line_suffix():
#        return {"CR":b"\r","CRLF":b"\r\n","LF":b"\n","NONE":b""}[enter_mode]
#
#    def send_bytes(data:bytes, tag="TX", safe=False, local_echo_line=None):
#        if not data: return
#        per_char_delay = char_delay if char_delay>0 else (SAFE_SCRIPT_CHAR_DELAY_MS if safe else 0)
#        if local_echo_line and SCRIPT_LOCAL_ECHO and not QUIET_RX:
#            print(local_echo_line)
#        if per_char_delay>0 and len(data)>1:
#            for i,b in enumerate(data):
#                with send_lock:
#                    try: ser.write(bytes([b])); ser.flush()
#                    except serial.SerialException as e:
#                        print(f"[ERR] TX failed: {e}"); return
#                if TX_HEX and not QUIET_RX:
#                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
#                if log_file:
#                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
#                    except: pass
#                if i<len(data)-1:
#                    time.sleep(per_char_delay/1000.0)
#        else:
#            with send_lock:
#                try: ser.write(data); ser.flush()
#                except serial.SerialException as e:
#                    print(f"[ERR] TX failed: {e}"); return
#            if TX_HEX and not QUIET_RX:
#                print(f"[{tag} HEX] {format_hex(data)}")
#            if log_file:
#                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
#                except: pass
#        if line_delay>0 and tag.startswith("TX"):
#            time.sleep(line_delay/1000.0)
#
#    class ScriptContext:
#        def __init__(self):
#            self.last_prompt_seq=get_prompt_seq()
#            self.first_send=True
#        def wait_ready_if_needed(self):
#            if not SCRIPT_WAIT_PROMPT: return
#            if self.first_send:
#                self.first_send=False; return
#            prev=self.last_prompt_seq
#            self.last_prompt_seq=wait_for_next_prompt(prev, SCRIPT_PROMPT_TIMEOUT_SEC)
#        def note_after_send(self): pass
#
#    def send_line(text:str, safe=False, local_echo=None, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        try:
#            body=text.encode(ENCODING,errors="replace")
#        except Exception as e:
#            print(f"[WARN] Encode failed: {e}"); return
#        send_bytes(body+line_suffix(), safe=safe, local_echo_line=local_echo)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    def send_enter_only(safe=False, script_ctx=None):
#        if safe and script_ctx: script_ctx.wait_ready_if_needed()
#        send_bytes(line_suffix(), tag="TX-EMPTY", safe=safe)
#        if safe and script_ctx: script_ctx.note_after_send()
#
#    # Slot + combo storage
#    slot_cmds={k:None for k in ALL_SLOTS}
#    load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
#    global_combos={}
#    load_global_combos(COMBO_SAVE_FILE, global_combos)  # digits only
#
#    # Display helpers
#    def show_slots():
#        print("[SLOTS] ---------------------------")
#        for k in DIGIT_SLOTS + LETTER_SLOTS:
#            v=slot_cmds.get(k)
#            if v is None:
#                print(f" {k}: (empty)")
#            else:
#                t=v.get("type")
#                if t=="enter": print(f" {k}: <ENTER>")
#                elif t=="combo": print(f" {k}: <COMBO {v.get('seq','')}>")
#                else:
#                    data=v.get("data","")
#                    first=data.splitlines()[0] if data else ""
#                    more=" ..." if "\n" in data else ""
#                    print(f" {k}: {first[:60]}{more}")
#        print("[SLOTS] ---------------------------")
#
#    def show_global_combos():
#        print("[COMBOS] (digits 0-9) -------------")
#        if not global_combos:
#            print(" (none)")
#        else:
#            for d in DIGIT_SLOTS:
#                if d in global_combos:
#                    print(f" {d}: {global_combos[d]}")
#                else:
#                    print(f" {d}: (empty)")
#        print("[COMBOS] ---------------------------")
#
#    def show_all():  # combined for Ctrl+S
#        show_slots()
#        show_global_combos()
#
#    def print_help():
#        print("""[HELP]
#Slots (0-9,a-z):
#  /setX <text>      Set slot X (multi-line token <ENTER> inserts blank line send)
#  /comboX <seq>     Slot X = sequence of slots (0-9a-z)
#  /enterX           Slot X = single ENTER
#  /clrX             Clear slot X
#  oX                Play slot X (safe script mode)
#  /slots            Show all slots
#  /slotsave         Save slots
#  /slotload         Load slots
#
#Global digit combos (names restricted to 0..9):
#  /cset <digit> <seq>   Define/replace digit combo
#  /clist                List digit combos
#  /crun <digit>         Run single digit combo
#  /cclear <digit>       Clear that combo
#  /crun_all             Run all defined digit combos
#  /csave /cload         Save / load combos
#
#Delays & modes:
#  /delay [ms]           Per-char interactive TX delay (0 disable)
#  /scriptdelay [ms]     Minimum per-char delay for scripted (when /delay=0)
#  /linedelay [ms]       Delay after a line is sent
#  /hex on|off           Show TX data in HEX
#  /scriptecho on|off    Local echo for script batch executes
#
#General:
#  /help                 Show this help
#  /quit                 Exit
#
#Hotkeys (Windows):
#  Ctrl+0..9 / Ctrl+a..z  Play slot
#  Ctrl+S                 Show slots + digit combos
#  C + B + <digit>        Run that single digit combo (C+B alone does nothing)
#  C + L                  List digit combos only
#""")
#
#    # Recursive slot execution
#    def play_slot_recursive(idx_char, depth, visited, script_ctx):
#        if depth>40:
#            print("[PLAY] Depth limit")
#            return
#        if idx_char not in slot_cmds:
#            print(f"[PLAY] Slot {idx_char} not found")
#            return
#        v=slot_cmds[idx_char]
#        if v is None:
#            print(f"[PLAY] Slot {idx_char} empty")
#            return
#        if id(v) in visited:
#            print(f"[PLAY] Cycle detected at slot {idx_char}")
#            return
#        visited.add(id(v))
#        t=v.get("type")
#        if t=="enter":
#            send_enter_only(safe=True, script_ctx=script_ctx)
#        elif t=="combo":
#            seq=v.get("seq","")
#            for c in seq:
#                if c in slot_cmds:
#                    play_slot_recursive(c, depth+1, visited, script_ctx)
#        else:
#            data=v.get("data","")
#            parts=data.split(TOKEN_ENTER)
#            for pi, segment in enumerate(parts):
#                lines=segment.splitlines()
#                if not lines and segment=="":
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#                for line in lines:
#                    if line.strip()=="" and line!="":
#                        send_enter_only(safe=True, script_ctx=script_ctx)
#                    elif line!="":
#                        send_line(line, safe=True,
#                                  local_echo=f"[RUN] {line}" if SCRIPT_LOCAL_ECHO else None,
#                                  script_ctx=script_ctx)
#                if pi < len(parts)-1:
#                    send_enter_only(safe=True, script_ctx=script_ctx)
#        visited.remove(id(v))
#
#    def play_slot(key):
#        if key not in slot_cmds:
#            print(f"[PLAY] Slot {key} invalid")
#            return
#        print(f"[PLAY] Slot {key}")
#        ctx=ScriptContext()
#        play_slot_recursive(key,0,set(),ctx)
#
#    def run_global_combo(digit_name):
#        if digit_name not in global_combos:
#            print(f"[COMBO] Digit {digit_name} undefined")
#            return
#        seq=global_combos[digit_name]
#        print(f"[COMBO] Run {digit_name}: {seq}")
#        ctx=ScriptContext()
#        for c in seq:
#            if c in slot_cmds:
#                play_slot_recursive(c,0,set(),ctx)
#
#    def run_all_global_combos():
#        defined=[d for d in DIGIT_SLOTS if d in global_combos]
#        if not defined:
#            print("[COMBO] No digit combos defined")
#            return
#        print("[COMBO] Run ALL digit combos:")
#        ctx=ScriptContext()
#        for d in defined:
#            seq=global_combos[d]
#            print(f"  -> {d}: {seq}")
#            for c in seq:
#                if c in slot_cmds:
#                    play_slot_recursive(c,0,set(),ctx)
#
#    def run_single_combo_via_hotkey(digit):
#        if digit in global_combos:
#            print(f"[COMBO] (Hotkey) {digit}")
#            run_global_combo(digit)
#        else:
#            print(f"[COMBO] (Hotkey) {digit} undefined")
#
#    # Hotkey thread (no longer triggers all combos on C+B)
#    stop_hotkey=threading.Event()
#    hotkey_thread=None
#    if os.name == 'nt':
#        try:
#            hotkey_thread=HotkeyThread(
#                play_callback=play_slot,
#                show_all_callback=show_all,
#                combo_list_callback=show_global_combos,
#                run_single_combo_callback=run_single_combo_via_hotkey,
#                stop_event=stop_hotkey
#            )
#            hotkey_thread.start()
#        except Exception as e:
#            print(f"[WARN] Hotkey thread failed: {e}")
#
#    # Command loop
#    try:
#        while True:
#            try:
#                line=input()
#            except EOFError:
#                break
#            stripped=line.strip()
#
#            if stripped=="/help":
#                print_help()
#                continue
#
#            # Delays / modes
#            if stripped.startswith("/delay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[DELAY] {char_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        char_delay=v; print(f"[DELAY] -> {char_delay} ms"); persist_user()
#                    except: print(f"[DELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptdelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTDELAY] {SAFE_SCRIPT_CHAR_DELAY_MS} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        SAFE_SCRIPT_CHAR_DELAY_MS=v; print(f"[SCRIPTDELAY] -> {SAFE_SCRIPT_CHAR_DELAY_MS} ms"); persist_user()
#                    except: print(f"[SCRIPTDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/linedelay"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[LINEDELAY] {line_delay} ms")
#                else:
#                    try:
#                        v=float(parts[1]); assert v>=0
#                        line_delay=v; print(f"[LINEDELAY] -> {line_delay} ms"); persist_user()
#                    except: print(f"[LINEDELAY] Invalid: {parts[1]}")
#                continue
#            if stripped.startswith("/scriptecho"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[SCRIPTECHO] {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        SCRIPT_LOCAL_ECHO=(arg=="on"); print(f"[SCRIPTECHO] -> {'ON' if SCRIPT_LOCAL_ECHO else 'OFF'}"); persist_user()
#                    else: print("[SCRIPTECHO] Use: /scriptecho on|off")
#                continue
#            if stripped.startswith("/hex"):
#                parts=stripped.split(None,1)
#                if len(parts)==1: print(f"[HEX] {'ON' if TX_HEX else 'OFF'}")
#                else:
#                    arg=parts[1].lower()
#                    if arg in ("on","off"):
#                        TX_HEX=(arg=="on"); print(f"[HEX] -> {'ON' if TX_HEX else 'OFF'}"); persist_user()
#                    else: print("[HEX] Use: /hex on|off")
#                continue
#
#            # Slots persistence
#            if stripped=="/slotsave":
#                save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds); continue
#            if stripped=="/slotload":
#                load_slots_from_file(SLOTS_SAVE_FILE,slot_cmds); continue
#
#            # Combos (digits only)
#            if stripped=="/clist":
#                show_global_combos(); continue
#            if stripped.startswith("/cset "):
#                parts=stripped.split(None,2)
#                if len(parts)<3:
#                    print("[COMBO] Usage: /cset <digit> <seq>")
#                else:
#                    digit=parts[1]
#                    if not (digit.isdigit() and len(digit)==1):
#                        print("[COMBO] Name must be a single digit (0-9)")
#                    else:
#                        seq="".join(ch for ch in parts[2] if ch.isalnum())
#                        global_combos[digit]=seq
#                        print(f"[COMBO] {digit} = {seq}")
#                        if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                continue
#            if stripped.startswith("/crun "):
#                digit=stripped.split(None,1)[1].strip()
#                run_global_combo(digit); continue
#            if stripped.startswith("/cclear "):
#                digit=stripped.split(None,1)[1].strip()
#                if digit in global_combos:
#                    del global_combos[digit]; print(f"[COMBO] Cleared {digit}")
#                    if AUTO_SAVE_COMBOS: save_global_combos(COMBO_SAVE_FILE,global_combos)
#                else:
#                    print(f"[COMBO] {digit} not defined")
#                continue
#            if stripped=="/csave":
#                save_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/cload":
#                load_global_combos(COMBO_SAVE_FILE,global_combos); continue
#            if stripped=="/crun_all":
#                run_all_global_combos(); continue
#
#            # General
#            if stripped=="/quit":
#                print("[INFO] Quit")
#                break
#            if stripped=="/slots":
#                show_slots(); continue
#
#            # Slot definitions
#            if stripped.startswith("/enter") and len(stripped)==7:
#                key=stripped[6].lower()
#                if key in slot_cmds:
#                    slot_cmds[key]={"type":"enter"}
#                    print(f"[SET] Slot {key} = <ENTER>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            if stripped.startswith("/combo") and len(stripped)>=7:
#                key=stripped[6].lower()
#                if key in slot_cmds:
#                    parts=line.split(None,1)
#                    seq=""
#                    if len(parts)>1:
#                        seq="".join(ch for ch in parts[1] if ch.isalnum())
#                    slot_cmds[key]={"type":"combo","seq":seq}
#                    print(f"[SET] Slot {key} = <COMBO {seq}>")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            if stripped.startswith("/set") and len(stripped)>=5:
#                key=stripped[4].lower()
#                if key in slot_cmds:
#                    parts=line.split(None,1)
#                    data=parts[1] if len(parts)>1 else ""
#                    slot_cmds[key]={"type":"raw","data":data}
#                    print(f"[SET] Slot {key} raw length={len(data)}")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            if stripped.startswith("/clr") and len(stripped)==5:
#                key=stripped[4].lower()
#                if key in slot_cmds:
#                    slot_cmds[key]=None
#                    print(f"[CLR] Slot {key} cleared")
#                    if AUTO_SAVE_SLOTS: save_slots_to_file(SLOTS_SAVE_FILE,slot_cmds)
#                continue
#
#            # Play slot (safe)
#            if len(stripped)==2 and stripped[0] in ('o','O'):
#                key=stripped[1].lower()
#                if key in slot_cmds:
#                    play_slot(key)
#                continue
#
#            # Blank line => just ENTER
#            if line=="":
#                send_enter_only(safe=False)
#                continue
#
#            # Interactive normal line
#            try:
#                body=line.encode(ENCODING,errors="replace")
#            except Exception as e:
#                print(f"[WARN] Encode failed: {e}")
#                continue
#            send_bytes(body+line_suffix(), safe=False, tag="TX")
#
#    except KeyboardInterrupt:
#        print("\n[INFO] KeyboardInterrupt")
#    finally:
#        persist_user()
#        if 'hotkey_thread' in locals() and hotkey_thread:
#            stop_hotkey.set()
#            hotkey_thread.join(timeout=0.5)
#        reader.stop()
#        time.sleep(0.05)
#        try: ser.close()
#        except: pass
#        if 'log_file' in locals() and log_file:
#            try: log_file.close()
#            except: pass
#        print("[INFO] Exit")
#
#if __name__ == "__main__":
#    main()

















    """

250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass
250903_0002_jason_save_cmd_pass





#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import serial
import threading
import time
import os
import json
from datetime import datetime

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# ================== 預設設定 (可互動修改) ==================
PORT             = "COM5"
BAUD             = 115200
PARITY_NAME      = "none"    # none / even / odd / mark / space
DATA_BITS        = 8
STOP_BITS        = 1
FLOW_CTRL        = "none"    # none / rtscts / dsrdtr / x
ENTER_MODE       = "CR"      # CR / CRLF / LF / NONE
ENCODING         = "utf-8"
TIMEOUT          = 0.05
CHAR_DELAY_MS    = 0         # 預設逐字延遲 (ms)；可用 /delay 指令動態修改
LINE_DELAY_MS    = 0
ASSERT_DTR       = False
ASSERT_RTS       = False
CLEAR_BUFF_ON_OPEN = False

TX_HEX           = True
HEX_DUMP_RX      = False
RAW_RX           = False
QUIET_RX         = False

LOG_PATH         = None
INI_PATH         = None
NO_BANNER        = False

# 互動選項
INTERACTIVE_SELECT = True
REMEMBER_LAST      = True
LAST_FILE_NAME     = ".last_port"

# 快捷槽
MAX_SLOTS = 10   # 0~9

# 槽持久化
SLOTS_SAVE_FILE   = ".slot_cmds.json"
AUTO_SAVE_SLOTS   = True      # /setN 或 /clrN 後自動存檔
SHOW_SAVE_MESSAGE = True      # 可改 False 降低輸出

# 輪詢熱鍵 (Ctrl+S / Ctrl+0..9)
HOTKEY_POLL_INTERVAL_SEC = 0.05

def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k,v = line.split("=",1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

# -------- 槽 JSON 載入/儲存 -------- #
def load_slots_from_file(path, slot_dict):
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for k in slot_dict.keys():
            if k in data and (data[k] is None or isinstance(data[k], str)):
                slot_dict[k] = data[k]
                changed = True
        if changed:
            print(f"[SLOTS] 讀取 {path} 完成")
    except Exception as e:
        print(f"[SLOTS] 讀取失敗: {e}")

def save_slots_to_file(path, slot_dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(slot_dict, f, ensure_ascii=False, indent=2)
        if SHOW_SAVE_MESSAGE:
            print(f"[SLOTS] 已儲存 -> {path}")
    except Exception as e:
        print(f"[SLOTS] 儲存失敗: {e}")

# -------- 接收執行緒 (保持原樣風格) -------- #
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

def load_last_port():
    if not REMEMBER_LAST: return None
    try:
        if os.path.isfile(LAST_FILE_NAME):
            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
                p=f.read().strip()
                if p: return p
    except:
        pass
    return None

def save_last_port(p):
    if not REMEMBER_LAST: return
    try:
        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
            f.write(p.strip())
    except:
        pass

def interactive_select_port(default_port):
    port = default_port
    baud = BAUD
    parity_name = PARITY_NAME
    data_bits = DATA_BITS
    stop_bits = STOP_BITS
    flow_ctrl = FLOW_CTRL
    enter_mode = ENTER_MODE

    last = load_last_port()
    if last:
        default_port = last

    if not INTERACTIVE_SELECT:
        return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

    print("=== 串口互動設定 (Enter=預設) ===")
    if list_ports:
        ports = list(list_ports.comports())
        if ports:
            print("可用埠:")
            for idx,p in enumerate(ports,1):
                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
        else:
            print("未偵測到 COM")
    else:
        print("無法列舉埠 (serial.tools.list_ports 缺)")

    val = input(f"Port [{default_port}]: ").strip()
    if val: port = val
    val = input(f"Baud [{baud}]: ").strip()
    if val.isdigit(): baud = int(val)
    plist = ["none","even","odd","mark","space"]
    val = input(f"Parity {plist} [{parity_name}]: ").strip().lower()
    if val in plist: parity_name = val
    val = input(f"Data bits (7/8) [{data_bits}]: ").strip()
    if val in ("7","8"): data_bits = int(val)
    val = input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
    if val in ("1","2"): stop_bits = int(val)
    flist=["none","rtscts","dsrdtr","x"]
    val = input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
    if val in flist: flow_ctrl = val
    emlist=["CR","CRLF","LF","NONE"]
    val = input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
    if val in emlist: enter_mode = val

    save_last_port(port)
    return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

# -------- 熱鍵輪詢執行緒 (Windows) -------- #
class HotkeyThread(threading.Thread):
    def __init__(self, play_callback, show_callback, stop_event):
        super().__init__(daemon=True)
        self.play_callback = play_callback
        self.show_callback = show_callback
        self.stop_event = stop_event
        import ctypes
        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.VK_CTRL  = 0x11
        self.VK_S     = 0x53
        self.VK_0_9   = list(range(0x30, 0x3A))
        self.VK_NUM_0_9 = list(range(0x60, 0x6A))
        self.prev_digit_down = {vk: False for vk in self.VK_0_9 + self.VK_NUM_0_9}
        self.prev_s_down = False

    def key_down(self, vk):
        return (self.user32.GetAsyncKeyState(vk) & 0x8000) != 0

    def run(self):
        while not self.stop_event.is_set():
            ctrl = self.key_down(self.VK_CTRL)

            s_now = ctrl and self.key_down(self.VK_S)
            if s_now and not self.prev_s_down:
                print()
                self.show_callback()
            self.prev_s_down = s_now

            if ctrl:
                for vk in self.VK_0_9 + self.VK_NUM_0_9:
                    now = self.key_down(vk)
                    if now and not self.prev_digit_down[vk]:
                        if 0x30 <= vk <= 0x39:
                            digit = chr(vk)
                        else:
                            digit = str(vk - 0x60)
                        print()
                        self.play_callback(digit)
                    self.prev_digit_down[vk] = now
            else:
                for vk in self.prev_digit_down:
                    self.prev_digit_down[vk] = False
                self.prev_s_down = False

            time.sleep(HOTKEY_POLL_INTERVAL_SEC)

def main():
    cfg = parse_ini(INI_PATH) if INI_PATH else {}

    init_port = f"COM{cfg['ComPort']}" if "ComPort" in cfg else PORT
    init_baud = cfg.get("BaudRate", BAUD)
    init_parity = (cfg.get("Parity", PARITY_NAME)).lower()
    init_data_bits = cfg.get("DataBit", DATA_BITS)
    init_stop_bits = cfg.get("StopBit", STOP_BITS)
    init_flow = cfg.get("FlowCtrl", FLOW_CTRL).lower()
    init_enter = cfg.get("CRSend", ENTER_MODE).upper()
    if init_enter not in ("CR","CRLF","LF","NONE"):
        init_enter = "CR"

    (port, baud, parity_name, data_bits, stop_bits_val,
     fc, enter_mode) = interactive_select_port(init_port)

    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity = parity_map.get(parity_name.lower(), serial.PARITY_NONE)
    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    if fc in ("rtscts","hard"):
        rtscts, dsrdtr, xonxoff = True, False, False
    elif fc == "dsrdtr":
        rtscts, dsrdtr, xonxoff = False, True, False
    elif fc == "x":
        rtscts, dsrdtr, xonxoff = False, False, True
    else:
        rtscts = dsrdtr = xonxoff = False

    char_delay = cfg.get("DelayPerChar", CHAR_DELAY_MS)
    line_delay = cfg.get("DelayPerLine", LINE_DELAY_MS)

    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=TIMEOUT,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    clear_flag = cfg.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN
    if clear_flag:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not NO_BANNER:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if TX_HEX:
            print("[INFO] TX HEX=ON")
        print("[INFO] 指令: /setN <cmd>  /clrN  /slots  oN 播放  /delay [ms]  /slotsave  /slotload  /quit")
        print("---------------------------------------------")

    log_file = None
    if LOG_PATH:
        try:
            log_file = open(LOG_PATH,"a",encoding="utf-8")
            print(f"[INFO] Log -> {LOG_PATH}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    reader = SerialReaderThread(
        ser,
        encoding=ENCODING,
        hex_dump=HEX_DUMP_RX,
        raw=RAW_RX,
        log_file=log_file,
        quiet=QUIET_RX
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data: return
        if char_delay > 0 and len(data) > 1:
            for i,b in enumerate(data):
                with send_lock:
                    try:
                        ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}")
                        return
                if TX_HEX and not QUIET_RX:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try:
                        log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except: pass
                if i < len(data)-1:
                    time.sleep(char_delay/1000.0)
        else:
            with send_lock:
                try:
                    ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}")
                    return
            if TX_HEX and not QUIET_RX:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except: pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay/1000.0)

    def send_line(text: str):
        try:
            body = text.encode(ENCODING, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    # 槽初始化 + 載入
    slot_cmds = {str(i): None for i in range(MAX_SLOTS)}
    load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)

    def show_slots():
        print("[SLOTS] ---------------------------")
        for k in sorted(slot_cmds.keys(), key=int):
            v = slot_cmds[k]
            if not v:
                print(f" {k}: (empty)")
            else:
                first = v.splitlines()[0]
                more = " ..." if "\n" in v else ""
                print(f" {k}: {first[:60]}{more}")
        print("[SLOTS] ---------------------------")

    def play_slot(k):
        v = slot_cmds.get(k)
        if not v:
            print(f"[PLAY] 槽 {k} 空")
            return
        lines = v.splitlines()
        print(f"[PLAY] 槽 {k} ({len(lines)} 行)")
        for ln in lines:
            send_line(ln)

    # 熱鍵
    stop_hotkey = threading.Event()
    hotkey_thread = None
    if os.name == 'nt':
        try:
            hotkey_thread = HotkeyThread(play_callback=play_slot,
                                         show_callback=show_slots,
                                         stop_event=stop_hotkey)
            hotkey_thread.start()
            print("[INFO] 熱鍵輪詢啟動 (Ctrl+S / Ctrl+0..9)")
        except Exception as e:
            print(f"[WARN] 熱鍵執行緒啟動失敗: {e}")

    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            stripped = line.strip()

            # ---- 動態調整逐字延遲 (/delay) ----
            if stripped.startswith("/delay"):
                parts = stripped.split(None, 1)
                if len(parts) == 1:
                    print(f"[DELAY] 目前逐字延遲 = {char_delay} ms (0=關閉逐字延遲)")
                else:
                    val = parts[1].strip()
                    try:
                        newv = float(val)
                        if newv < 0: raise ValueError
                        char_delay = newv
                        print(f"[DELAY] 設定逐字延遲 = {char_delay} ms")
                    except ValueError:
                        print(f"[DELAY] 無效數值: {val}")
                continue

            # 手動存檔
            if stripped == "/slotsave":
                save_slots_to_file(SLOTS_SAVE_FILE, slot_cmds)
                continue
            # 手動重新讀
            if stripped == "/slotload":
                load_slots_from_file(SLOTS_SAVE_FILE, slot_cmds)
                continue

            if stripped == "/quit":
                print("[INFO] /quit")
                break
            if stripped == "/slots":
                show_slots()
                continue
            if stripped.startswith("/set") and len(stripped) >= 5 and stripped[4].isdigit():
                slot = stripped[4]
                parts = line.split(None,1)
                if len(parts) < 2:
                    slot_cmds[slot] = ""
                    print(f"[SET] 槽 {slot} = (empty)")
                else:
                    slot_cmds[slot] = parts[1]
                    text = slot_cmds[slot]
                    print(f"[SET] 槽 {slot} 設定 (長度={len(text)} 行數={text.count(chr(10))+1})")
                if AUTO_SAVE_SLOTS:
                    save_slots_to_file(SLOTS_SAVE_FILE, slot_cmds)
                continue
            if stripped.startswith("/clr") and len(stripped) == 5 and stripped[4].isdigit():
                slot = stripped[4]
                slot_cmds[slot] = None
                print(f"[CLR] 槽 {slot} 已清除")
                if AUTO_SAVE_SLOTS:
                    save_slots_to_file(SLOTS_SAVE_FILE, slot_cmds)
                continue
            if len(stripped) == 2 and stripped[0] in ('o','O') and stripped[1].isdigit():
                play_slot(stripped[1])
                continue
            if line == "":
                send_bytes(line_suffix(), tag="TX-EMPTY")
                continue
            send_line(line)

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C")
    finally:
        if hotkey_thread:
            stop_hotkey.set()
            hotkey_thread.join(timeout=0.5)
        reader.stop()
        time.sleep(0.05)
        try: ser.close()
        except: pass
        if log_file:
            try: log_file.close()
            except: pass
        print("[INFO] 結束。")

if __name__ == "__main__":
    main()





    """



















    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass
250903_0001_uart_tx_send_delay_set_pass







#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import serial
import threading
import time
import os
from datetime import datetime

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# ================== 預設設定 (可互動修改) ==================
PORT             = "COM5"
BAUD             = 115200
PARITY_NAME      = "none"    # none / even / odd / mark / space
DATA_BITS        = 8
STOP_BITS        = 1
FLOW_CTRL        = "none"    # none / rtscts / dsrdtr / x
ENTER_MODE       = "CR"      # CR / CRLF / LF / NONE
ENCODING         = "utf-8"
TIMEOUT          = 0.05
CHAR_DELAY_MS    = 0         # 預設逐字延遲 (ms)；可用 /delay 指令動態修改
LINE_DELAY_MS    = 0
ASSERT_DTR       = False
ASSERT_RTS       = False
CLEAR_BUFF_ON_OPEN = False

TX_HEX           = True
HEX_DUMP_RX      = False
RAW_RX           = False
QUIET_RX         = False

LOG_PATH         = None
INI_PATH         = None
NO_BANNER        = False

# 互動選項
INTERACTIVE_SELECT = True
REMEMBER_LAST      = True
LAST_FILE_NAME     = ".last_port"

# 快捷槽
MAX_SLOTS = 10   # 0~9

# 輪詢熱鍵 (Ctrl+S / Ctrl+0..9)
HOTKEY_POLL_INTERVAL_SEC = 0.05

def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k,v = line.split("=",1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

# -------- 接收執行緒 (保持原樣風格) -------- #
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

def load_last_port():
    if not REMEMBER_LAST: return None
    try:
        if os.path.isfile(LAST_FILE_NAME):
            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
                p=f.read().strip()
                if p: return p
    except:
        pass
    return None

def save_last_port(p):
    if not REMEMBER_LAST: return
    try:
        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
            f.write(p.strip())
    except:
        pass

def interactive_select_port(default_port):
    port = default_port
    baud = BAUD
    parity_name = PARITY_NAME
    data_bits = DATA_BITS
    stop_bits = STOP_BITS
    flow_ctrl = FLOW_CTRL
    enter_mode = ENTER_MODE

    last = load_last_port()
    if last:
        default_port = last

    if not INTERACTIVE_SELECT:
        return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

    print("=== 串口互動設定 (Enter=預設) ===")
    if list_ports:
        ports = list(list_ports.comports())
        if ports:
            print("可用埠:")
            for idx,p in enumerate(ports,1):
                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
        else:
            print("未偵測到 COM")
    else:
        print("無法列舉埠 (serial.tools.list_ports 缺)")

    val = input(f"Port [{default_port}]: ").strip()
    if val: port = val
    val = input(f"Baud [{baud}]: ").strip()
    if val.isdigit(): baud = int(val)
    plist = ["none","even","odd","mark","space"]
    val = input(f"Parity {plist} [{parity_name}]: ").strip().lower()
    if val in plist: parity_name = val
    val = input(f"Data bits (7/8) [{data_bits}]: ").strip()
    if val in ("7","8"): data_bits = int(val)
    val = input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
    if val in ("1","2"): stop_bits = int(val)
    flist=["none","rtscts","dsrdtr","x"]
    val = input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
    if val in flist: flow_ctrl = val
    emlist=["CR","CRLF","LF","NONE"]
    val = input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
    if val in emlist: enter_mode = val

    save_last_port(port)
    return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

# -------- 熱鍵輪詢執行緒 (Windows) -------- #
class HotkeyThread(threading.Thread):
    def __init__(self, play_callback, show_callback, stop_event):
        super().__init__(daemon=True)
        self.play_callback = play_callback
        self.show_callback = show_callback
        self.stop_event = stop_event
        import ctypes
        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.VK_CTRL  = 0x11
        self.VK_S     = 0x53
        self.VK_0_9   = list(range(0x30, 0x3A))
        self.VK_NUM_0_9 = list(range(0x60, 0x6A))
        self.prev_digit_down = {vk: False for vk in self.VK_0_9 + self.VK_NUM_0_9}
        self.prev_s_down = False

    def key_down(self, vk):
        return (self.user32.GetAsyncKeyState(vk) & 0x8000) != 0

    def run(self):
        while not self.stop_event.is_set():
            ctrl = self.key_down(self.VK_CTRL)

            s_now = ctrl and self.key_down(self.VK_S)
            if s_now and not self.prev_s_down:
                print()
                self.show_callback()
            self.prev_s_down = s_now

            if ctrl:
                for vk in self.VK_0_9 + self.VK_NUM_0_9:
                    now = self.key_down(vk)
                    if now and not self.prev_digit_down[vk]:
                        if 0x30 <= vk <= 0x39:
                            digit = chr(vk)
                        else:
                            digit = str(vk - 0x60)
                        print()
                        self.play_callback(digit)
                    self.prev_digit_down[vk] = now
            else:
                for vk in self.prev_digit_down:
                    self.prev_digit_down[vk] = False
                self.prev_s_down = False

            time.sleep(HOTKEY_POLL_INTERVAL_SEC)

def main():
    cfg = parse_ini(INI_PATH) if INI_PATH else {}

    init_port = f"COM{cfg['ComPort']}" if "ComPort" in cfg else PORT
    init_baud = cfg.get("BaudRate", BAUD)
    init_parity = (cfg.get("Parity", PARITY_NAME)).lower()
    init_data_bits = cfg.get("DataBit", DATA_BITS)
    init_stop_bits = cfg.get("StopBit", STOP_BITS)
    init_flow = cfg.get("FlowCtrl", FLOW_CTRL).lower()
    init_enter = cfg.get("CRSend", ENTER_MODE).upper()
    if init_enter not in ("CR","CRLF","LF","NONE"):
        init_enter = "CR"

    (port, baud, parity_name, data_bits, stop_bits_val,
     fc, enter_mode) = interactive_select_port(init_port)

    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity = parity_map.get(parity_name.lower(), serial.PARITY_NONE)
    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    if fc in ("rtscts","hard"):
        rtscts, dsrdtr, xonxoff = True, False, False
    elif fc == "dsrdtr":
        rtscts, dsrdtr, xonxoff = False, True, False
    elif fc == "x":
        rtscts, dsrdtr, xonxoff = False, False, True
    else:
        rtscts = dsrdtr = xonxoff = False

    char_delay = cfg.get("DelayPerChar", CHAR_DELAY_MS)
    line_delay = cfg.get("DelayPerLine", LINE_DELAY_MS)

    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=TIMEOUT,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    clear_flag = cfg.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN
    if clear_flag:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not NO_BANNER:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if TX_HEX:
            print("[INFO] TX HEX=ON")
        print("[INFO] 指令: /setN <cmd>  /clrN  /slots  oN 播放  /delay [ms]  (查/設逐字延遲)  /quit 離開")
        print("---------------------------------------------")

    log_file = None
    if LOG_PATH:
        try:
            log_file = open(LOG_PATH,"a",encoding="utf-8")
            print(f"[INFO] Log -> {LOG_PATH}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    reader = SerialReaderThread(
        ser,
        encoding=ENCODING,
        hex_dump=HEX_DUMP_RX,
        raw=RAW_RX,
        log_file=log_file,
        quiet=QUIET_RX
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data: return
        # 使用當前 char_delay
        if char_delay > 0 and len(data) > 1:
            for i,b in enumerate(data):
                with send_lock:
                    try:
                        ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}")
                        return
                if TX_HEX and not QUIET_RX:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try:
                        log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except: pass
                if i < len(data)-1:
                    time.sleep(char_delay/1000.0)
        else:
            with send_lock:
                try:
                    ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}")
                    return
            if TX_HEX and not QUIET_RX:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except: pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay/1000.0)

    def send_line(text: str):
        try:
            body = text.encode(ENCODING, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    # 槽
    slot_cmds = {str(i): None for i in range(MAX_SLOTS)}

    def show_slots():
        print("[SLOTS] ---------------------------")
        for k in sorted(slot_cmds.keys(), key=int):
            v = slot_cmds[k]
            if not v:
                print(f" {k}: (empty)")
            else:
                first = v.splitlines()[0]
                more = " ..." if "\n" in v else ""
                print(f" {k}: {first[:60]}{more}")
        print("[SLOTS] ---------------------------")

    def play_slot(k):
        v = slot_cmds.get(k)
        if not v:
            print(f"[PLAY] 槽 {k} 空")
            return
        lines = v.splitlines()
        print(f"[PLAY] 槽 {k} ({len(lines)} 行)")
        for ln in lines:
            send_line(ln)

    # 熱鍵
    stop_hotkey = threading.Event()
    hotkey_thread = None
    if os.name == 'nt':
        try:
            hotkey_thread = HotkeyThread(play_callback=play_slot,
                                         show_callback=show_slots,
                                         stop_event=stop_hotkey)
            hotkey_thread.start()
            print("[INFO] 熱鍵輪詢啟動 (Ctrl+S / Ctrl+0..9)")
        except Exception as e:
            print(f"[WARN] 熱鍵執行緒啟動失敗: {e}")

    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            stripped = line.strip()

            # ---- 動態調整逐字延遲 (/delay) ----
            if stripped.startswith("/delay"):
                parts = stripped.split(None, 1)
                if len(parts) == 1:
                    print(f"[DELAY] 目前逐字延遲 = {char_delay} ms (0=關閉逐字延遲)")
                else:
                    val = parts[1].strip()
                    try:
                        newv = float(val)
                        if newv < 0: raise ValueError
                        char_delay = newv
                        print(f"[DELAY] 設定逐字延遲 = {char_delay} ms")
                    except ValueError:
                        print(f"[DELAY] 無效數值: {val}")
                continue

            if stripped == "/quit":
                print("[INFO] /quit")
                break
            if stripped == "/slots":
                show_slots()
                continue
            if stripped.startswith("/set") and len(stripped) >= 5 and stripped[4].isdigit():
                slot = stripped[4]
                parts = line.split(None,1)
                if len(parts) < 2:
                    slot_cmds[slot] = ""
                    print(f"[SET] 槽 {slot} = (empty)")
                else:
                    slot_cmds[slot] = parts[1]
                    text = slot_cmds[slot]
                    print(f"[SET] 槽 {slot} 設定 (長度={len(text)} 行數={text.count(chr(10))+1})")
                continue
            if stripped.startswith("/clr") and len(stripped) == 5 and stripped[4].isdigit():
                slot = stripped[4]
                slot_cmds[slot] = None
                print(f"[CLR] 槽 {slot} 已清除")
                continue
            if len(stripped) == 2 and stripped[0] in ('o','O') and stripped[1].isdigit():
                play_slot(stripped[1])
                continue
            if line == "":
                send_bytes(line_suffix(), tag="TX-EMPTY")
                continue
            send_line(line)

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C")
    finally:
        if hotkey_thread:
            stop_hotkey.set()
            hotkey_thread.join(timeout=0.5)
        reader.stop()
        time.sleep(0.05)
        try: ser.close()
        except: pass
        if log_file:
            try: log_file.close()
            except: pass
        print("[INFO] 結束。")

if __name__ == "__main__":
    main()




    """

















    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass
250902_0006_set_cmd_ctrl+N_pass



#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import serial
import threading
import time
import os
from datetime import datetime

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# ================== 預設設定 (可互動修改) ==================
PORT             = "COM5"
BAUD             = 115200
PARITY_NAME      = "none"    # none / even / odd / mark / space
DATA_BITS        = 8
STOP_BITS        = 1
FLOW_CTRL        = "none"    # none / rtscts / dsrdtr / x
ENTER_MODE       = "CR"      # CR / CRLF / LF / NONE
ENCODING         = "utf-8"
TIMEOUT          = 0.05
CHAR_DELAY_MS    = 0
LINE_DELAY_MS    = 0
ASSERT_DTR       = False
ASSERT_RTS       = False
CLEAR_BUFF_ON_OPEN = False

TX_HEX           = True
HEX_DUMP_RX      = False
RAW_RX           = False
QUIET_RX         = False

LOG_PATH         = None
INI_PATH         = None
NO_BANNER        = False

# 互動選項
INTERACTIVE_SELECT = True
REMEMBER_LAST      = True
LAST_FILE_NAME     = ".last_port"

# 快捷槽
MAX_SLOTS = 10   # 0~9

# 輪詢熱鍵 (Ctrl+S / Ctrl+0..9)
HOTKEY_POLL_INTERVAL_SEC = 0.05

def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k,v = line.split("=",1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

# -------- 接收執行緒 (保持原樣風格) -------- #
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

def load_last_port():
    if not REMEMBER_LAST: return None
    try:
        if os.path.isfile(LAST_FILE_NAME):
            with open(LAST_FILE_NAME,"r",encoding="utf-8") as f:
                p=f.read().strip()
                if p: return p
    except:
        pass
    return None

def save_last_port(p):
    if not REMEMBER_LAST: return
    try:
        with open(LAST_FILE_NAME,"w",encoding="utf-8") as f:
            f.write(p.strip())
    except:
        pass

def interactive_select_port(default_port):
    port = default_port
    baud = BAUD
    parity_name = PARITY_NAME
    data_bits = DATA_BITS
    stop_bits = STOP_BITS
    flow_ctrl = FLOW_CTRL
    enter_mode = ENTER_MODE

    last = load_last_port()
    if last:
        default_port = last

    if not INTERACTIVE_SELECT:
        return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

    print("=== 串口互動設定 (Enter=預設) ===")
    if list_ports:
        ports = list(list_ports.comports())
        if ports:
            print("可用埠:")
            for idx,p in enumerate(ports,1):
                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
        else:
            print("未偵測到 COM")
    else:
        print("無法列舉埠 (serial.tools.list_ports 缺)")

    val = input(f"Port [{default_port}]: ").strip()
    if val: port = val
    val = input(f"Baud [{baud}]: ").strip()
    if val.isdigit(): baud = int(val)
    plist = ["none","even","odd","mark","space"]
    val = input(f"Parity {plist} [{parity_name}]: ").strip().lower()
    if val in plist: parity_name = val
    val = input(f"Data bits (7/8) [{data_bits}]: ").strip()
    if val in ("7","8"): data_bits = int(val)
    val = input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
    if val in ("1","2"): stop_bits = int(val)
    flist=["none","rtscts","dsrdtr","x"]
    val = input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
    if val in flist: flow_ctrl = val
    emlist=["CR","CRLF","LF","NONE"]
    val = input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
    if val in emlist: enter_mode = val

    save_last_port(port)
    return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

# -------- 熱鍵輪詢執行緒 (Windows) -------- #
class HotkeyThread(threading.Thread):
    def __init__(self, play_callback, show_callback, stop_event):
        super().__init__(daemon=True)
        self.play_callback = play_callback
        self.show_callback = show_callback
        self.stop_event = stop_event
        import ctypes
        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        # 虛擬鍵碼
        self.VK_CTRL  = 0x11
        self.VK_S     = 0x53
        self.VK_0_9   = list(range(0x30, 0x3A))      # '0'..'9'
        self.VK_NUM_0_9 = list(range(0x60, 0x6A))    # NumPad 0..9
        # 狀態緩存避免重複觸發
        self.prev_digit_down = {vk: False for vk in self.VK_0_9 + self.VK_NUM_0_9}
        self.prev_s_down = False

    def key_down(self, vk):
        return (self.user32.GetAsyncKeyState(vk) & 0x8000) != 0

    def run(self):
        while not self.stop_event.is_set():
            ctrl = self.key_down(self.VK_CTRL)

            # Ctrl+S
            s_now = ctrl and self.key_down(self.VK_S)
            if s_now and not self.prev_s_down:
                print()
                self.show_callback()
            self.prev_s_down = s_now

            # Ctrl+digits
            if ctrl:
                for vk in self.VK_0_9 + self.VK_NUM_0_9:
                    now = self.key_down(vk)
                    if now and not self.prev_digit_down[vk]:
                        # 轉成數字字元
                        if 0x30 <= vk <= 0x39:
                            digit = chr(vk)
                        else:
                            digit = str(vk - 0x60)
                        print()
                        self.play_callback(digit)
                    self.prev_digit_down[vk] = now
            else:
                # reset down states (避免 Ctrl 放開後再次按下不觸發)
                for vk in self.prev_digit_down:
                    self.prev_digit_down[vk] = False
                self.prev_s_down = False

            time.sleep(HOTKEY_POLL_INTERVAL_SEC)

def main():
    cfg = parse_ini(INI_PATH) if INI_PATH else {}

    init_port = f"COM{cfg['ComPort']}" if "ComPort" in cfg else PORT
    init_baud = cfg.get("BaudRate", BAUD)
    init_parity = (cfg.get("Parity", PARITY_NAME)).lower()
    init_data_bits = cfg.get("DataBit", DATA_BITS)
    init_stop_bits = cfg.get("StopBit", STOP_BITS)
    init_flow = cfg.get("FlowCtrl", FLOW_CTRL).lower()
    init_enter = cfg.get("CRSend", ENTER_MODE).upper()
    if init_enter not in ("CR","CRLF","LF","NONE"):
        init_enter = "CR"

    (port, baud, parity_name, data_bits, stop_bits_val,
     fc, enter_mode) = interactive_select_port(init_port)

    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity = parity_map.get(parity_name.lower(), serial.PARITY_NONE)
    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    if fc in ("rtscts","hard"):
        rtscts, dsrdtr, xonxoff = True, False, False
    elif fc == "dsrdtr":
        rtscts, dsrdtr, xonxoff = False, True, False
    elif fc == "x":
        rtscts, dsrdtr, xonxoff = False, False, True
    else:
        rtscts = dsrdtr = xonxoff = False

    char_delay = cfg.get("DelayPerChar", CHAR_DELAY_MS)
    line_delay = cfg.get("DelayPerLine", LINE_DELAY_MS)

    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=TIMEOUT,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    clear_flag = cfg.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN
    if clear_flag:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not NO_BANNER:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate} Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if TX_HEX:
            print("[INFO] TX HEX=ON")
        print("[INFO] 指令: /setN <cmd>  /clrN  /slots  oN 播放  Ctrl+S 槽列表  Ctrl+0..9 播放槽  /quit 離開")
        print("---------------------------------------------")

    log_file = None
    if LOG_PATH:
        try:
            log_file = open(LOG_PATH,"a",encoding="utf-8")
            print(f"[INFO] Log -> {LOG_PATH}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    reader = SerialReaderThread(
        ser,
        encoding=ENCODING,
        hex_dump=HEX_DUMP_RX,
        raw=RAW_RX,
        log_file=log_file,
        quiet=QUIET_RX
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data: return
        if char_delay > 0 and len(data) > 1:
            for i,b in enumerate(data):
                with send_lock:
                    try:
                        ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}")
                        return
                if TX_HEX and not QUIET_RX:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try:
                        log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except: pass
                if i < len(data)-1:
                    time.sleep(char_delay/1000.0)
        else:
            with send_lock:
                try:
                    ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}")
                    return
            if TX_HEX and not QUIET_RX:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except: pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay/1000.0)

    def send_line(text: str):
        try:
            body = text.encode(ENCODING, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    # 槽
    slot_cmds = {str(i): None for i in range(MAX_SLOTS)}

    def show_slots():
        print("[SLOTS] ---------------------------")
        for k in sorted(slot_cmds.keys(), key=int):
            v = slot_cmds[k]
            if not v:
                print(f" {k}: (empty)")
            else:
                first = v.splitlines()[0]
                more = " ..." if "\n" in v else ""
                print(f" {k}: {first[:60]}{more}")
        print("[SLOTS] ---------------------------")

    def play_slot(k):
        v = slot_cmds.get(k)
        if not v:
            print(f"[PLAY] 槽 {k} 空")
            return
        lines = v.splitlines()
        print(f"[PLAY] 槽 {k} ({len(lines)} 行)")
        for ln in lines:
            send_line(ln)

    # 啟動熱鍵輪詢 (只在 Windows)
    stop_hotkey = threading.Event()
    hotkey_thread = None
    if os.name == 'nt':
        try:
            hotkey_thread = HotkeyThread(play_callback=play_slot,
                                         show_callback=show_slots,
                                         stop_event=stop_hotkey)
            hotkey_thread.start()
            print("[INFO] 熱鍵輪詢啟動 (Ctrl+S / Ctrl+0..9)")
        except Exception as e:
            print(f"[WARN] 熱鍵執行緒啟動失敗: {e}")

    try:
        # 行模式輸入
        while True:
            try:
                line = input()
            except EOFError:
                break
            stripped = line.strip()

            if stripped == "/quit":
                print("[INFO] /quit")
                break
            if stripped == "/slots":
                show_slots()
                continue
            # /setN <cmd>
            if stripped.startswith("/set") and len(stripped) >= 5 and stripped[4].isdigit():
                slot = stripped[4]
                parts = line.split(None,1)
                if len(parts) < 2:
                    slot_cmds[slot] = ""
                    print(f"[SET] 槽 {slot} = (empty)")
                else:
                    slot_cmds[slot] = parts[1]
                    text = slot_cmds[slot]
                    print(f"[SET] 槽 {slot} 設定 (長度={len(text)} 行數={text.count(chr(10))+1})")
                continue
            # /clrN
            if stripped.startswith("/clr") and len(stripped) == 5 and stripped[4].isdigit():
                slot = stripped[4]
                slot_cmds[slot] = None
                print(f"[CLR] 槽 {slot} 已清除")
                continue
            # oN 播放
            if len(stripped) == 2 and stripped[0] in ('o','O') and stripped[1].isdigit():
                play_slot(stripped[1])
                continue
            # 空行
            if line == "":
                send_bytes(line_suffix(), tag="TX-EMPTY")
                continue
            # 一般行
            send_line(line)

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C")
    finally:
        if hotkey_thread:
            stop_hotkey.set()
            hotkey_thread.join(timeout=0.5)
        reader.stop()
        time.sleep(0.05)
        try: ser.close()
        except: pass
        if log_file:
            try: log_file.close()
            except: pass
        print("[INFO] 結束。")

if __name__ == "__main__":
    main()






    """




















    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass
250902_0005_set_cmd_oN_pass


#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import serial
import threading
import time
import os
from datetime import datetime

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# ================== 預設設定 (可互動修改) ==================
PORT             = "COM5"
BAUD             = 115200
PARITY_NAME      = "none"
DATA_BITS        = 8
STOP_BITS        = 1
FLOW_CTRL        = "none"
ENTER_MODE       = "CR"      # CR / CRLF / LF / NONE
ENCODING         = "utf-8"
TIMEOUT          = 0.05
CHAR_DELAY_MS    = 0
LINE_DELAY_MS    = 0
ASSERT_DTR       = False
ASSERT_RTS       = False
CLEAR_BUFF_ON_OPEN = False

TX_HEX           = True
HEX_DUMP_RX      = False
RAW_RX           = False
QUIET_RX         = False

LOG_PATH         = None
INI_PATH         = None
NO_BANNER        = False

INTERACTIVE_SELECT = True
REMEMBER_LAST      = True
LAST_FILE_NAME     = ".last_port"

# 快捷槽 0~9
MAX_SLOTS = 10
# /setN <command>  設定槽
# /clrN            清除槽
# /slots           顯示槽
# Ctrl+S           顯示槽
# Ctrl+0..Ctrl+9   送出槽
# /quit            離開

def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

def load_last_port():
    if not REMEMBER_LAST: return None
    try:
        if os.path.isfile(LAST_FILE_NAME):
            with open(LAST_FILE_NAME, "r", encoding="utf-8") as f:
                p = f.read().strip()
                if p:
                    return p
    except:
        pass
    return None

def save_last_port(p):
    if not REMEMBER_LAST: return
    try:
        with open(LAST_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(p.strip())
    except:
        pass

def interactive_select_port(default_port):
    port = default_port
    baud = BAUD
    parity_name = PARITY_NAME
    data_bits = DATA_BITS
    stop_bits = STOP_BITS
    flow_ctrl = FLOW_CTRL
    enter_mode = ENTER_MODE

    last = load_last_port()
    if last:
        default_port = last

    if not INTERACTIVE_SELECT:
        return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

    print("=== 串口互動設定 (Enter=預設) ===")
    if list_ports:
        ports = list(list_ports.comports())
        if ports:
            print("可用埠:")
            for idx, p in enumerate(ports, 1):
                print(f"  {idx}. {p.device:<10} {p.description} ({p.hwid})")
        else:
            print("未偵測到 COM")
    else:
        print("無法列舉埠")

    val = input(f"Port [{default_port}]: ").strip()
    if val: port = val
    val = input(f"Baud [{baud}]: ").strip()
    if val.isdigit(): baud = int(val)
    plist = ["none","even","odd","mark","space"]
    val = input(f"Parity {plist} [{parity_name}]: ").strip().lower()
    if val in plist: parity_name = val
    val = input(f"Data bits (7/8) [{data_bits}]: ").strip()
    if val in ("7","8"): data_bits = int(val)
    val = input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
    if val in ("1","2"): stop_bits = int(val)
    flist = ["none","rtscts","dsrdtr","x"]
    val = input(f"FlowCtrl {flist} [{flow_ctrl}]: ").strip().lower()
    if val in flist: flow_ctrl = val
    emlist = ["CR","CRLF","LF","NONE"]
    val = input(f"Enter mode {emlist} [{enter_mode}]: ").strip().upper()
    if val in emlist: enter_mode = val

    save_last_port(port)
    return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

def main():
    cfg = parse_ini(INI_PATH) if INI_PATH else {}
    init_port = f"COM{cfg['ComPort']}" if "ComPort" in cfg else PORT
    init_baud = cfg.get("BaudRate", BAUD)
    init_parity = (cfg.get("Parity", PARITY_NAME)).lower()
    init_data_bits = cfg.get("DataBit", DATA_BITS)
    init_stop_bits = cfg.get("StopBit", STOP_BITS)
    init_flow = cfg.get("FlowCtrl", FLOW_CTRL).lower()
    init_enter = cfg.get("CRSend", ENTER_MODE).upper()
    if init_enter not in ("CR","CRLF","LF","NONE"):
        init_enter = "CR"

    (port, baud, parity_name, data_bits, stop_bits_val,
     fc, enter_mode) = interactive_select_port(init_port)

    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity = parity_map.get(parity_name.lower(), serial.PARITY_NONE)
    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    if fc in ("rtscts","hard"):
        rtscts, dsrdtr, xonxoff = True, False, False
    elif fc == "dsrdtr":
        rtscts, dsrdtr, xonxoff = False, True, False
    elif fc == "x":
        rtscts, dsrdtr, xonxoff = False, False, True
    else:
        rtscts = dsrdtr = xonxoff = False

    char_delay = cfg.get("DelayPerChar", CHAR_DELAY_MS)
    line_delay = cfg.get("DelayPerLine", LINE_DELAY_MS)

    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=TIMEOUT,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    clear_flag = cfg.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN
    if clear_flag:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not NO_BANNER:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate}  Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if TX_HEX:
            print("[INFO] TX HEX=ON")
        print("[INFO] Ctrl+S 槽列表  Ctrl+0..9 播放槽  /setN <cmd> 設定槽  /clrN 清除槽  /slots 顯示槽  /quit 離開")
        print("---------------------------------------------")

    log_file = None
    if LOG_PATH:
        try:
            log_file = open(LOG_PATH, "a", encoding="utf-8")
            print(f"[INFO] Log -> {LOG_PATH}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    reader = SerialReaderThread(
        ser,
        encoding=ENCODING,
        hex_dump=HEX_DUMP_RX,
        raw=RAW_RX,
        log_file=log_file,
        quiet=QUIET_RX
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data:
            return
        if char_delay > 0 and len(data) > 1:
            for i, b in enumerate(data):
                with send_lock:
                    try: ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}")
                        return
                if TX_HEX and not QUIET_RX:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except: pass
                if i < len(data)-1:
                    time.sleep(char_delay/1000.0)
        else:
            with send_lock:
                try: ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}")
                    return
            if TX_HEX and not QUIET_RX:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except: pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay/1000.0)

    def send_line(text: str):
        try:
            body = text.encode(ENCODING, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    slot_cmds = {str(i): None for i in range(MAX_SLOTS)}

    def show_slots():
        print("[SLOTS] ---------------------------")
        for k in sorted(slot_cmds.keys(), key=int):
            v = slot_cmds[k]
            if v is None:
                print(f" {k}: (empty)")
            else:
                first = v.splitlines()[0]
                more = " ..." if "\n" in v else ""
                print(f" {k}: {first[:60]}{more}")
        print("[SLOTS] ---------------------------")

    def play_slot(k):
        v = slot_cmds.get(k)
        if not v:
            print(f"[PLAY] 槽 {k} 空")
            return
        lines = v.splitlines()
        print(f"[PLAY] 槽 {k} ({len(lines)} 行)")
        for ln in lines:
            send_line(ln)

    # 逐鍵模式 (Windows)
    key_mode = os.name == "nt" and sys.stdin.isatty()
    if key_mode:
        import msvcrt, ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        VK_CONTROL = 0x11
        def ctrl_down():
            return (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0
        line_buf = []
        try:
            while True:
                ch = msvcrt.getwch()
                if ch == '\x03':  # Ctrl+C
                    print("\n[INFO] Ctrl+C")
                    break
                if ch == '\x13':  # Ctrl+S
                    print()
                    show_slots()
                    continue
                if ch.isdigit() and ctrl_down():
                    print()
                    play_slot(ch)
                    continue
                if ch in ('\r', '\n'):
                    line = "".join(line_buf)
                    line_buf = []
                    stripped = line.strip()

                    if stripped == "/quit":
                        print("[INFO] /quit")
                        break
                    if stripped == "/slots":
                        show_slots()
                        continue
                    # /setN <cmd>
                    if stripped.startswith("/set") and len(stripped) >= 5 and stripped[4].isdigit():
                        slot = stripped[4]
                        rest = line.split(None,1)
                        if len(rest) < 2:
                            slot_cmds[slot] = ""
                            print(f"[SET] 槽 {slot} = (empty)")
                        else:
                            cmd_text = rest[1]
                            slot_cmds[slot] = cmd_text
                            print(f"[SET] 槽 {slot} 設定 (長度 {len(cmd_text)} 行 {cmd_text.count(chr(10))+1})")
                        continue
                    # /clrN
                    if stripped.startswith("/clr") and len(stripped) == 5 and stripped[4].isdigit():
                        slot = stripped[4]
                        slot_cmds[slot] = None
                        print(f"[CLR] 槽 {slot} 已清除")
                        continue
                    # oN 播放
                    if len(stripped) == 2 and stripped[0] in ('o','O') and stripped[1].isdigit():
                        play_slot(stripped[1])
                        continue
                    # 空行
                    if line == "":
                        send_bytes(line_suffix(), tag="TX-EMPTY")
                        continue
                    send_line(line)
                    continue
                if ch == '\x08':  # Backspace
                    if line_buf:
                        line_buf.pop()
                        sys.stdout.write('\b \b')
                        sys.stdout.flush()
                    continue
                if ch == '\x1b':  # ESC 清行
                    while line_buf:
                        line_buf.pop()
                        sys.stdout.write('\b \b')
                    sys.stdout.flush()
                    continue
                if ch >= ' ':
                    line_buf.append(ch)
                    sys.stdout.write(ch)
                    sys.stdout.flush()
                    continue
        except KeyboardInterrupt:
            pass
    else:
        # 非逐鍵回退
        try:
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                stripped = line.strip()
                if stripped == "/quit":
                    print("[INFO] /quit")
                    break
                if stripped == "/slots":
                    show_slots()
                    continue
                if stripped.startswith("/set") and len(stripped) >=5 and stripped[4].isdigit():
                    slot = stripped[4]
                    parts = line.split(None,1)
                    if len(parts) < 2:
                        slot_cmds[slot] = ""
                        print(f"[SET] 槽 {slot} = (empty)")
                    else:
                        slot_cmds[slot] = parts[1]
                        print(f"[SET] 槽 {slot} 設定 (長度 {len(parts[1])})")
                    continue
                if stripped.startswith("/clr") and len(stripped)==5 and stripped[4].isdigit():
                    slot = stripped[4]
                    slot_cmds[slot] = None
                    print(f"[CLR] 槽 {slot} 已清除")
                    continue
                if len(stripped) == 2 and stripped[0] in ('o','O') and stripped[1].isdigit():
                    play_slot(stripped[1])
                    continue
                if line == "":
                    send_bytes(line_suffix(), tag="TX-EMPTY")
                    continue
                send_line(line)
        except KeyboardInterrupt:
            pass

    reader.stop()
    time.sleep(0.05)
    try: ser.close()
    except: pass
    if log_file:
        try: log_file.close()
        except: pass
    print("[INFO] 結束。")

if __name__ == "__main__":
    main()






    """














    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass
250902_0004_set_com_pass


#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import serial
import threading
import time
import os
from datetime import datetime

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# ================== 預設設定 (可互動修改) ==================
PORT             = "COM5"
BAUD             = 115200
PARITY_NAME      = "none"    # none / even / odd / mark / space
DATA_BITS        = 8         # 7 或 8
STOP_BITS        = 1         # 1 或 2
FLOW_CTRL        = "none"    # none / rtscts / dsrdtr / x
ENTER_MODE       = "CR"      # CR / CRLF / LF / NONE
ENCODING         = "utf-8"
TIMEOUT          = 0.05
CHAR_DELAY_MS    = 0
LINE_DELAY_MS    = 0
ASSERT_DTR       = False
ASSERT_RTS       = False
CLEAR_BUFF_ON_OPEN = False

TX_HEX           = True
HEX_DUMP_RX      = False
RAW_RX           = False
QUIET_RX         = False

LOG_PATH         = None
INI_PATH         = None
NO_BANNER        = False

# ==== 互動選項 ====
INTERACTIVE_SELECT = True     # True 啟動時列出可用 COM 讓使用者選
REMEMBER_LAST      = True     # 記住上次選擇 (寫入 .last_port)
LAST_FILE_NAME     = ".last_port"
# ===========================================================

def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

# -------- 接收執行緒 (保持原樣風格, 未加 CR 行緩衝處理) -------- #
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

def load_last_port():
    if not REMEMBER_LAST: return None
    try:
        if os.path.isfile(LAST_FILE_NAME):
            with open(LAST_FILE_NAME, "r", encoding="utf-8") as f:
                p = f.read().strip()
                if p:
                    return p
    except:
        pass
    return None

def save_last_port(p):
    if not REMEMBER_LAST: return
    try:
        with open(LAST_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(p.strip())
    except:
        pass

def interactive_select_port(default_port):
    port = default_port
    baud = BAUD
    parity_name = PARITY_NAME
    data_bits = DATA_BITS
    stop_bits = STOP_BITS
    flow_ctrl = FLOW_CTRL
    enter_mode = ENTER_MODE

    last = load_last_port()
    if last:
        default_port = last

    if not INTERACTIVE_SELECT:
        return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

    print("=== 串口互動設定 (直接 Enter 使用預設) ===")
    # 列出可用 COM
    candidates = []
    if list_ports:
        ports = list(list_ports.comports())
        if ports:
            print("可用埠:")
            for idx, p in enumerate(ports, 1):
                desc = f"{p.description}"
                hwid = f"{p.hwid}"
                print(f"  {idx}. {p.device:<10} {desc} ({hwid})")
            candidates = [p.device for p in ports]
        else:
            print("未偵測到可用 COM (仍可手動輸入)")
    else:
        print("無法匯入 serial.tools.list_ports (略過列舉)")

    # Port
    while True:
        p_in = input(f"Port [{default_port}]: ").strip()
        if not p_in:
            port = default_port
            break
        port = p_in
        break

    # Baud
    b_in = input(f"Baud [{baud}]: ").strip()
    if b_in.isdigit():
        baud = int(b_in)

    # Parity
    p_list = ["none","even","odd","mark","space"]
    p_in = input(f"Parity {p_list} [{parity_name}]: ").strip().lower()
    if p_in in p_list:
        parity_name = p_in

    # Data bits
    db_in = input(f"Data bits (7/8) [{data_bits}]: ").strip()
    if db_in in ("7","8"):
        data_bits = int(db_in)

    # Stop bits
    sb_in = input(f"Stop bits (1/2) [{stop_bits}]: ").strip()
    if sb_in in ("1","2"):
        stop_bits = int(sb_in)

    # Flow ctrl
    fc_list = ["none","rtscts","dsrdtr","x"]
    fc_in = input(f"FlowCtrl {fc_list} [{flow_ctrl}]: ").strip().lower()
    if fc_in in fc_list:
        flow_ctrl = fc_in

    # Enter mode
    em_list = ["CR","CRLF","LF","NONE"]
    em_in = input(f"Enter mode {em_list} [{enter_mode}]: ").strip().upper()
    if em_in in em_list:
        enter_mode = em_in

    print("=== 設定完成 ===")
    save_last_port(port)
    return port, baud, parity_name, data_bits, stop_bits, flow_ctrl, enter_mode

def main():
    cfg = parse_ini(INI_PATH) if INI_PATH else {}

    # default from constants or INI
    init_port = f"COM{cfg['ComPort']}" if "ComPort" in cfg else PORT
    init_baud = cfg.get("BaudRate", BAUD)
    init_parity = (cfg.get("Parity", PARITY_NAME)).lower()
    init_data_bits = cfg.get("DataBit", DATA_BITS)
    init_stop_bits = cfg.get("StopBit", STOP_BITS)
    init_flow = cfg.get("FlowCtrl", FLOW_CTRL).lower()
    init_enter = cfg.get("CRSend", ENTER_MODE).upper()
    if init_enter not in ("CR","CRLF","LF","NONE"):
        init_enter = "CR"

    (port, baud, parity_name, data_bits, stop_bits_val,
     fc, enter_mode) = interactive_select_port(init_port)

    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity = parity_map.get(parity_name.lower(), serial.PARITY_NONE)

    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    if fc in ("rtscts","hard"):
        rtscts, dsrdtr, xonxoff = True, False, False
    elif fc == "dsrdtr":
        rtscts, dsrdtr, xonxoff = False, True, False
    elif fc == "x":
        rtscts, dsrdtr, xonxoff = False, False, True
    else:
        rtscts = dsrdtr = xonxoff = False

    char_delay = cfg.get("DelayPerChar", CHAR_DELAY_MS)
    line_delay = cfg.get("DelayPerLine", LINE_DELAY_MS)

    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=TIMEOUT,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    clear_flag = cfg.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN
    if clear_flag:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not NO_BANNER:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate}  Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if TX_HEX:
            print("[INFO] TX HEX=ON (可觀察空行是否只送 0D)")
        print("[INFO] 空行=只送行尾。輸入 /quit 離開。Ctrl+C 亦可。")
        print("---------------------------------------------")

    log_file = None
    if LOG_PATH:
        try:
            log_file = open(LOG_PATH, "a", encoding="utf-8")
            print(f"[INFO] Log -> {LOG_PATH}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    reader = SerialReaderThread(
        ser,
        encoding=ENCODING,
        hex_dump=HEX_DUMP_RX,
        raw=RAW_RX,
        log_file=log_file,
        quiet=QUIET_RX
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data:
            return
        if char_delay > 0 and len(data) > 1:
            for i, b in enumerate(data):
                with send_lock:
                    try:
                        ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}")
                        return
                if TX_HEX and not QUIET_RX:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try:
                        log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except:
                        pass
                if i < len(data) - 1:
                    time.sleep(char_delay / 1000.0)
        else:
            with send_lock:
                try:
                    ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}")
                    return
            if TX_HEX and not QUIET_RX:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except:
                    pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay / 1000.0)

    def send_line(text: str):
        try:
            body = text.encode(ENCODING, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "/quit":
                print("[INFO] /quit")
                break
            if line == "":
                send_bytes(line_suffix(), tag="TX-EMPTY")
                continue
            send_line(line)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        time.sleep(0.05)
        try: ser.close()
        except: pass
        if log_file:
            try: log_file.close()
            except: pass
        print("[INFO] 結束。")

if __name__ == "__main__":
    main()


    """




    """
250902_0002_output_tx_on_spyder_pass
250902_0003_output_tx_on_spyder_pass_use_anaconda_prompt_pass


import sys
import serial
import threading
import time
import os
from datetime import datetime

# ================== 可調整設定 (直接改這裡) ==================
PORT             = "COM5"
BAUD             = 115200
PARITY_NAME      = "none"    # none / even / odd / mark / space
DATA_BITS        = 8         # 7 或 8
STOP_BITS        = 1         # 1 或 2
FLOW_CTRL        = "none"    # none / rtscts / dsrdtr / x
ENTER_MODE       = "CR"      # CR / CRLF / LF / NONE
ENCODING         = "utf-8"
TIMEOUT          = 0.05      # 讀取 timeout 秒
CHAR_DELAY_MS    = 0         # 每字節延遲 (ms) 0=無
LINE_DELAY_MS    = 0         # 每行送完延遲 (ms)
ASSERT_DTR       = False
ASSERT_RTS       = False
CLEAR_BUFF_ON_OPEN = False

TX_HEX           = True      # 強制顯示送出 HEX (空行也會顯示)
HEX_DUMP_RX      = False     # True: 以 HEX 顯示接收
RAW_RX           = False     # True: raw bytes 不解碼
QUIET_RX         = False     # True: 不顯示接收

LOG_PATH         = None      # 例如 "session_hex.log" 或 None 不紀錄
INI_PATH         = None      # 例如 "TERATERM.INI" 或 None
NO_BANNER        = False
# =============================================================

# -------- 工具 -------- #
def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k,v = line.split("=",1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

# -------- 接收執行緒 (保持原樣風格, 未加 CR 行緩衝處理) -------- #
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

# -------- 主程式 (無 argparse, 直接用常數/INI) -------- #
def main():
    # 讀 INI (若設定)
    cfg = parse_ini(INI_PATH) if INI_PATH else {}

    # 端口/波特率 (INI 覆蓋)
    port = f"COM{cfg['ComPort']}" if "ComPort" in cfg else PORT
    baud = cfg.get("BaudRate", BAUD)

    # Parity
    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity_name = (cfg.get("Parity", PARITY_NAME)).lower()
    parity = parity_map.get(parity_name, serial.PARITY_NONE)

    # Data bits
    data_bits = cfg.get("DataBit", DATA_BITS)
    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS

    # Stop bits
    stop_bits_val = cfg.get("StopBit", STOP_BITS)
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    # Flow control
    fc = cfg.get("FlowCtrl", FLOW_CTRL).lower()
    if fc in ("rtscts","hard"):
        rtscts, dsrdtr, xonxoff = True, False, False
    elif fc == "dsrdtr":
        rtscts, dsrdtr, xonxoff = False, True, False
    elif fc == "x":
        rtscts, dsrdtr, xonxoff = False, False, True
    else:
        rtscts = dsrdtr = xonxoff = False

    # 行尾模式
    enter_mode = cfg.get("CRSend", ENTER_MODE).upper()
    if enter_mode not in ("CR","CRLF","LF","NONE"):
        enter_mode = "CR"

    # 延遲
    char_delay = cfg.get("DelayPerChar", CHAR_DELAY_MS)
    line_delay = cfg.get("DelayPerLine", LINE_DELAY_MS)

    # 開埠
    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=TIMEOUT,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    # 控制線
    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    # 清緩衝 (INI 或常數)
    clear_flag = cfg.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN
    if clear_flag:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not NO_BANNER:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate}  Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if TX_HEX:
            print("[INFO] TX HEX=ON (可觀察空行是否只送 0D)")
        print("[INFO] 空行=只送行尾。輸入 /quit 離開。Ctrl+C 亦可。")
        print("---------------------------------------------")

    # Log
    log_file = None
    if LOG_PATH:
        try:
            log_file = open(LOG_PATH, "a", encoding="utf-8")
            print(f"[INFO] Log -> {LOG_PATH}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    # 啟動接收
    reader = SerialReaderThread(
        ser,
        encoding=ENCODING,
        hex_dump=HEX_DUMP_RX,
        raw=RAW_RX,
        log_file=log_file,
        quiet=QUIET_RX
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data:
            return
        if char_delay > 0 and len(data) > 1:
            for i,b in enumerate(data):
                with send_lock:
                    try:
                        ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}")
                        return
                if TX_HEX and not QUIET_RX:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try:
                        log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except:
                        pass
                if i < len(data)-1:
                    time.sleep(char_delay/1000.0)
        else:
            with send_lock:
                try:
                    ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}")
                    return
            if TX_HEX and not QUIET_RX:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except:
                    pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay/1000.0)

    def send_line(text: str):
        try:
            body = text.encode(ENCODING, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "/quit":
                print("[INFO] /quit")
                break
            if line == "":
                # 空行 -> 只送行尾 (CR / CRLF / ...)
                send_bytes(line_suffix(), tag="TX-EMPTY")
                continue
            send_line(line)

    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        time.sleep(0.05)
        try:
            ser.close()
        except:
            pass
        if log_file:
            try: log_file.close()
            except: pass
        print("[INFO] 結束。")

if __name__ == "__main__":
    main()

    """ 











    """
250902_0002_output_tx_on_spyder_pass

import sys
import serial
import threading
import time
import os
from datetime import datetime

# ================== 可調整設定 (直接改這裡) ==================
PORT             = "COM5"
BAUD             = 115200
PARITY_NAME      = "none"    # none / even / odd / mark / space
DATA_BITS        = 8         # 7 或 8
STOP_BITS        = 1         # 1 或 2
FLOW_CTRL        = "none"    # none / rtscts / dsrdtr / x
ENTER_MODE       = "CR"      # CR / CRLF / LF / NONE
ENCODING         = "utf-8"
TIMEOUT          = 0.05      # 讀取 timeout 秒
CHAR_DELAY_MS    = 0         # 每字節延遲 (ms) 0=無
LINE_DELAY_MS    = 0         # 每行送完延遲 (ms)
ASSERT_DTR       = False
ASSERT_RTS       = False
CLEAR_BUFF_ON_OPEN = False

TX_HEX           = True      # 強制顯示送出 HEX (空行也會顯示)
HEX_DUMP_RX      = False     # True: 以 HEX 顯示接收
RAW_RX           = False     # True: raw bytes 不解碼
QUIET_RX         = False     # True: 不顯示接收

LOG_PATH         = None      # 例如 "session_hex.log" 或 None 不紀錄
INI_PATH         = None      # 例如 "TERATERM.INI" 或 None
NO_BANNER        = False
# =============================================================

# -------- 工具 -------- #
def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k,v = line.split("=",1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

# -------- 接收執行緒 (保持原樣風格, 未加 CR 行緩衝處理) -------- #
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

# -------- 主程式 (無 argparse, 直接用常數/INI) -------- #
def main():
    # 讀 INI (若設定)
    cfg = parse_ini(INI_PATH) if INI_PATH else {}

    # 端口/波特率 (INI 覆蓋)
    port = f"COM{cfg['ComPort']}" if "ComPort" in cfg else PORT
    baud = cfg.get("BaudRate", BAUD)

    # Parity
    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity_name = (cfg.get("Parity", PARITY_NAME)).lower()
    parity = parity_map.get(parity_name, serial.PARITY_NONE)

    # Data bits
    data_bits = cfg.get("DataBit", DATA_BITS)
    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS

    # Stop bits
    stop_bits_val = cfg.get("StopBit", STOP_BITS)
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    # Flow control
    fc = cfg.get("FlowCtrl", FLOW_CTRL).lower()
    if fc in ("rtscts","hard"):
        rtscts, dsrdtr, xonxoff = True, False, False
    elif fc == "dsrdtr":
        rtscts, dsrdtr, xonxoff = False, True, False
    elif fc == "x":
        rtscts, dsrdtr, xonxoff = False, False, True
    else:
        rtscts = dsrdtr = xonxoff = False

    # 行尾模式
    enter_mode = cfg.get("CRSend", ENTER_MODE).upper()
    if enter_mode not in ("CR","CRLF","LF","NONE"):
        enter_mode = "CR"

    # 延遲
    char_delay = cfg.get("DelayPerChar", CHAR_DELAY_MS)
    line_delay = cfg.get("DelayPerLine", LINE_DELAY_MS)

    # 開埠
    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=TIMEOUT,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    # 控制線
    try:
        if ASSERT_DTR: ser.setDTR(True)
        if ASSERT_RTS: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    # 清緩衝 (INI 或常數)
    clear_flag = cfg.get("ClearComBuffOnOpen","off").lower()=="on" or CLEAR_BUFF_ON_OPEN
    if clear_flag:
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not NO_BANNER:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate}  Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if TX_HEX:
            print("[INFO] TX HEX=ON (可觀察空行是否只送 0D)")
        print("[INFO] 空行=只送行尾。輸入 /quit 離開。Ctrl+C 亦可。")
        print("---------------------------------------------")

    # Log
    log_file = None
    if LOG_PATH:
        try:
            log_file = open(LOG_PATH, "a", encoding="utf-8")
            print(f"[INFO] Log -> {LOG_PATH}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    # 啟動接收
    reader = SerialReaderThread(
        ser,
        encoding=ENCODING,
        hex_dump=HEX_DUMP_RX,
        raw=RAW_RX,
        log_file=log_file,
        quiet=QUIET_RX
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data:
            return
        if char_delay > 0 and len(data) > 1:
            for i,b in enumerate(data):
                with send_lock:
                    try:
                        ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}")
                        return
                if TX_HEX and not QUIET_RX:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try:
                        log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except:
                        pass
                if i < len(data)-1:
                    time.sleep(char_delay/1000.0)
        else:
            with send_lock:
                try:
                    ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}")
                    return
            if TX_HEX and not QUIET_RX:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except:
                    pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay/1000.0)

    def send_line(text: str):
        try:
            body = text.encode(ENCODING, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "/quit":
                print("[INFO] /quit")
                break
            if line == "":
                # 空行 -> 只送行尾 (CR / CRLF / ...)
                send_bytes(line_suffix(), tag="TX-EMPTY")
                continue
            send_line(line)

    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        time.sleep(0.05)
        try:
            ser.close()
        except:
            pass
        if log_file:
            try: log_file.close()
            except: pass
        print("[INFO] 結束。")

if __name__ == "__main__":
    main()


    









runfile('C:/python_uart/uart_250902_0003_tx_pass_login_fail.py', wdir='C:/python_uart')
[INFO] 開啟 COM5 @ 115200  Data=8 Parity=none Stop=1
[INFO] Flow rtscts=False dsrdtr=False xonxoff=False
[INFO] Enter 行尾 = CR
[INFO] TX HEX=ON (可觀察空行是否只送 0D)
[INFO] 空行=只送行尾。輸入 /quit 離開。Ctrl+C 亦可。
---------------------------------------------

[TX-EMPTY HEX] 0D

AMI7E3C90528324 login: 
[TX HEX] 0A 41 4D 49 37 45 33 43 39 30 35 32 38 33 32 34 20 6C 6F 67 69 6E 3A 20 0D

AMI7E3C90528324 login: AMI7E3C90528324 login: 
Password: 
[TX HEX] 0A 41 4D 49 37 45 33 43 39 30 35 32 38 33 32 34 20 6C 6F 67 69 6E 3A 20 41 4D 49 37 45 33 43 39 30 35 32 38 33 32 34 20 6C 6F 67 69 6E 3A 20 0A 50 61 73 73 77 6F 72 64 3A 20 0D

90528324 login: AMI7E3C9052[7926 : 7926 CRITICAL][nss-rsvdusers.c:256]_nss_rsvdusers_getpwnam_r - Source Buffer is truncated.
8324 login: 
Password: 
[7926 : 7926 CRITICAL][pam_ipmi.c:144]User Name is restricted to 16 Bytes

[7926 : 7926 CRITICAL][pam_ldap.c:96]Get no Password:
[7926 : 7926 CRITICAL][active_session.c:60]Unable to get privilege of the user:AMI7E3C90528324 login: :Not able to register the session

[7926 : 7926 WARNING]SERIAL Login Failed from IP:127.0.0.1 user:AMI7E3C90528324 login: 

Login incorrect
AMI7E3C90528324 login:   
    
    
    
    
    
想要理解這段程式碼
因為我沒有輸入任何東西只有按下enter
應該要一直出現
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
才對

我只要用tx送出我用鍵盤輸入的文字


這段程式接收資料盡量不要動
保持完整風格
請給我完整程式檔案
我都用spyder
我不要指令 請給我直接就可以執行的程式碼
    """  













    
  
    """

import argparse
import sys
import serial
import threading
import time
import os
from datetime import datetime

# -------- 工具 -------- #
def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def parse_ini(path: str):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                k,v = line.split("=",1)
                k = k.strip(); v = v.strip()
                kl = k.lower()
                if kl in ("comport","baudrate","delayperchar","delayperline"):
                    try: out[k] = int(v)
                    except: pass
                elif kl in ("parity","databit","stopbit","flowctrl","crsend","clearcombuffonopen"):
                    out[k] = v
    except Exception as e:
        print(f"[WARN] 讀 INI 失敗: {e}")
    return out

# -------- 接收執行緒 (保持原樣風格) -------- #
class SerialReaderThread(threading.Thread):
    def __init__(self, ser, *, encoding, hex_dump, raw, log_file, quiet):
        super().__init__(daemon=True)
        self.ser = ser
        self.encoding = encoding
        self.hex_dump = hex_dump
        self.raw = raw
        self.log_file = log_file
        self.quiet = quiet
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running and self.ser.is_open:
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                print(f"[ERR] Serial exception: {e}")
                break
            if not data:
                continue
            if self.log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try:
                    self.log_file.write(f"[{ts}] RX {format_hex(data)}\n")
                    self.log_file.flush()
                except Exception:
                    pass
            if self.quiet:
                continue
            if self.hex_dump:
                print(f"[RX HEX] {format_hex(data)}")
            elif self.raw:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            else:
                try:
                    text = data.decode(self.encoding, errors="replace")
                except Exception:
                    text = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02X}" for b in data)
                print(text, end="", flush=True)

# -------- 主程式 -------- #
def main():
    ap = argparse.ArgumentParser(description="Simple TeraTerm-like UART")
    ap.add_argument("--ini", help="TERATERM.INI 路徑 (自動套用 ComPort/CRSend 等)")
    ap.add_argument("-p","--port", help="覆蓋或指定 COM，例如 COM5")
    ap.add_argument("-b","--baud", type=int, help="覆蓋或指定 BaudRate")
    ap.add_argument("--parity", choices=["none","even","odd","mark","space"])
    ap.add_argument("--data-bits", type=int, choices=[7,8])
    ap.add_argument("--stop-bits", type=int, choices=[1,2])
    ap.add_argument("--rtscts", action="store_true")
    ap.add_argument("--dsrdtr", action="store_true")
    ap.add_argument("--xonxoff", action="store_true")
    ap.add_argument("--enter", choices=["CR","CRLF","LF","NONE"],
                    help="覆蓋 INI CRSend；若無 INI 與此參數，預設 CR")
    ap.add_argument("--encoding", default="utf-8", help="接收/傳送字串使用的編碼 (預設 utf-8)")
    ap.add_argument("--tx-hex", action="store_true", help="顯示送出 HEX")
    ap.add_argument("--hex-dump", action="store_true", help="接收以 HEX 顯示")
    ap.add_argument("--raw", action="store_true", help="接收 raw bytes，不解碼")
    ap.add_argument("--log", help="記錄 RX/TX HEX")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--timeout", type=float, default=0.05, help="serial read timeout 秒")
    ap.add_argument("--char-delay", type=float, help="每字節延遲 (ms)，未指定則取 INI DelayPerChar")
    ap.add_argument("--line-delay", type=float, help="每行送完延遲 (ms)，未指定則取 INI DelayPerLine")
    ap.add_argument("--assert-dtr", action="store_true")
    ap.add_argument("--assert-rts", action="store_true")
    ap.add_argument("--no-banner", action="store_true")
    args = ap.parse_args()

    cfg = parse_ini(args.ini) if args.ini else {}

    # 端口
    port = args.port or (f"COM{cfg['ComPort']}" if "ComPort" in cfg else "COM5")
    # 波特率
    baud = args.baud or cfg.get("BaudRate") or 115200

    # Parity
    parity_map = {
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
        "none": serial.PARITY_NONE,
        "mark": serial.PARITY_MARK,
        "space": serial.PARITY_SPACE
    }
    parity_name = (args.parity or cfg.get("Parity","none")).lower()
    parity = parity_map.get(parity_name, serial.PARITY_NONE)

    # Data bits
    data_bits = args.data_bits or cfg.get("DataBit") or 8
    bytesize = serial.SEVENBITS if data_bits == 7 else serial.EIGHTBITS

    # Stop bits
    stop_bits_val = args.stop_bits or cfg.get("StopBit") or 1
    stopbits = serial.STOPBITS_TWO if stop_bits_val == 2 else serial.STOPBITS_ONE

    # Flow control
    if any([args.rtscts, args.dsrdtr, args.xonxoff]):
        rtscts = args.rtscts; dsrdtr = args.dsrdtr; xonxoff = args.xonxoff
    else:
        fc = cfg.get("FlowCtrl","none").lower()
        if fc in ("rtscts","hard"):
            rtscts, dsrdtr, xonxoff = True, False, False
        elif fc == "dsrdtr":
            rtscts, dsrdtr, xonxoff = False, True, False
        elif fc == "x":
            rtscts, dsrdtr, xonxoff = False, False, True
        else:
            rtscts = dsrdtr = xonxoff = False

    # 行尾
    if args.enter:
        enter_mode = args.enter
    else:
        enter_mode = cfg.get("CRSend","CR").upper()
        if enter_mode not in ("CR","CRLF","LF","NONE"):
            enter_mode = "CR"

    # 延遲
    char_delay = args.char_delay if args.char_delay is not None else cfg.get("DelayPerChar",0)
    line_delay = args.line_delay if args.line_delay is not None else cfg.get("DelayPerLine",0)

    # 開埠
    try:
        ser = serial.Serial(
            port,
            baud,
            timeout=args.timeout,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
            xonxoff=xonxoff,
            write_timeout=1
        )
    except serial.SerialException as e:
        print(f"[ERR] 無法開啟 {port}: {e}")
        return

    # 控制線
    try:
        if args.assert_dtr: ser.setDTR(True)
        if args.assert_rts: ser.setRTS(True)
    except Exception as e:
        print(f"[WARN] 設定 DTR/RTS 失敗: {e}")

    # Clear buffer
    if cfg.get("ClearComBuffOnOpen","off").lower()=="on":
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as e:
            print(f"[WARN] 清緩衝失敗: {e}")

    if not args.no_banner:
        print(f"[INFO] 開啟 {ser.port} @ {ser.baudrate}  Data={data_bits} Parity={parity_name} Stop={stop_bits_val}")
        print(f"[INFO] Flow rtscts={rtscts} dsrdtr={dsrdtr} xonxoff={xonxoff}")
        print(f"[INFO] Enter 行尾 = {enter_mode}")
        if char_delay or line_delay:
            print(f"[INFO] Delay char={char_delay}ms line={line_delay}ms")
        if args.tx_hex:
            print("[INFO] TX HEX=ON")
        print("[INFO] 空行=只送行尾。輸入 /quit 離開。Ctrl+C 亦可。")
        print("---------------------------------------------")

    # Log
    log_file = None
    if args.log:
        try:
            log_file = open(args.log, "a", encoding="utf-8")
            print(f"[INFO] Log -> {args.log}")
        except Exception as e:
            print(f"[WARN] 開啟 log 失敗: {e}")

    # 啟動接收執行緒
    reader = SerialReaderThread(
        ser,
        encoding=args.encoding,
        hex_dump=args.hex_dump,
        raw=args.raw,
        log_file=log_file,
        quiet=args.quiet
    )
    reader.start()

    send_lock = threading.Lock()

    def line_suffix():
        return {
            "CR": b"\r",
            "CRLF": b"\r\n",
            "LF": b"\n",
            "NONE": b""
        }[enter_mode]

    def send_bytes(data: bytes, tag="TX"):
        if not data:
            return
        if char_delay > 0 and len(data) > 1:
            for i,b in enumerate(data):
                with send_lock:
                    try:
                        ser.write(bytes([b])); ser.flush()
                    except serial.SerialException as e:
                        print(f"[ERR] 傳送失敗: {e}"); return
                if args.tx_hex and not args.quiet:
                    print(f"[{tag} HEX] {format_hex(bytes([b]))}")
                if log_file:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    try: log_file.write(f"[{ts}] {tag} {format_hex(bytes([b]))}\n"); log_file.flush()
                    except: pass
                if i < len(data)-1:
                    time.sleep(char_delay/1000.0)
        else:
            with send_lock:
                try:
                    ser.write(data); ser.flush()
                except serial.SerialException as e:
                    print(f"[ERR] 傳送失敗: {e}"); return
            if args.tx_hex and not args.quiet:
                print(f"[{tag} HEX] {format_hex(data)}")
            if log_file:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                try: log_file.write(f"[{ts}] {tag} {format_hex(data)}\n"); log_file.flush()
                except: pass
        if line_delay > 0 and tag.startswith("TX"):
            time.sleep(line_delay/1000.0)

    def send_line(text: str):
        try:
            body = text.encode(args.encoding, errors="replace")
        except Exception as e:
            print(f"[WARN] 編碼失敗: {e}")
            return
        send_bytes(body + line_suffix())

    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "/quit":
                print("[INFO] /quit")
                break
            if line == "":
                send_bytes(line_suffix(), tag="TX-EMPTY")
                continue
            send_line(line)

    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        time.sleep(0.05)
        try: ser.close()
        except: pass
        if log_file:
            try: log_file.close()
            except: pass
        print("[INFO] 結束。")

if __name__ == "__main__":
    main()

    









runfile('C:/python_uart/uart_250902_0003_tx_pass_login_fail.py', wdir='C:/python_uart')
[INFO] 開啟 COM5 @ 115200  Data=8 Parity=none Stop=1
[INFO] Flow rtscts=False dsrdtr=False xonxoff=False
[INFO] Enter 行尾 = CR
[INFO] TX HEX=ON (可觀察空行是否只送 0D)
[INFO] 空行=只送行尾。輸入 /quit 離開。Ctrl+C 亦可。
---------------------------------------------

[TX-EMPTY HEX] 0D

AMI7E3C90528324 login: 
[TX HEX] 0A 41 4D 49 37 45 33 43 39 30 35 32 38 33 32 34 20 6C 6F 67 69 6E 3A 20 0D

AMI7E3C90528324 login: AMI7E3C90528324 login: 
Password: 
[TX HEX] 0A 41 4D 49 37 45 33 43 39 30 35 32 38 33 32 34 20 6C 6F 67 69 6E 3A 20 41 4D 49 37 45 33 43 39 30 35 32 38 33 32 34 20 6C 6F 67 69 6E 3A 20 0A 50 61 73 73 77 6F 72 64 3A 20 0D

90528324 login: AMI7E3C9052[7926 : 7926 CRITICAL][nss-rsvdusers.c:256]_nss_rsvdusers_getpwnam_r - Source Buffer is truncated.
8324 login: 
Password: 
[7926 : 7926 CRITICAL][pam_ipmi.c:144]User Name is restricted to 16 Bytes

[7926 : 7926 CRITICAL][pam_ldap.c:96]Get no Password:
[7926 : 7926 CRITICAL][active_session.c:60]Unable to get privilege of the user:AMI7E3C90528324 login: :Not able to register the session

[7926 : 7926 WARNING]SERIAL Login Failed from IP:127.0.0.1 user:AMI7E3C90528324 login: 

Login incorrect
AMI7E3C90528324 login:   
    
    
    
    
    
想要理解這段程式碼
因為我沒有輸入任何東西只有按下enter
應該要一直出現
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
AMI7E3C90528324 login:
才對


原程式 + 開 tx-hex：觀察空行送出是否只有 0D。


這段程式接收資料盡量不要動
保持完整風格
請給我完整程式檔案
我都用spyder
我不要指令 請給我直接就可以執行的程式碼
    """  
  
    
  
    
  
    
  
    
  
    
  
    
  
    
    """

目前是有接收到資料
但按下enter bmc沒有反應
我用teraterm就可以正常接收和傳送資料

這段程式接收資料盡量不要動
保持完整風格
請給我完整程式檔案
    """
    
    
    
    """

請幫我加入可以用鍵盤透過uart傳輸資料
到bmc的功能

這段程式接收資料盡量不要動
保持完整風格
請給我完整程式檔案
    """
    
    
    
    
    
    
    
    
    
    
    