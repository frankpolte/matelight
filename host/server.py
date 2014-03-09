#!/usr/bin/env python

from socketserver import *
import socket
import struct
import zlib
from time import time, strftime, sleep
from collections import namedtuple, deque
import itertools
import threading
import random

from ctypes import *

import numpy as np

from matelight import sendframe, DISPLAY_WIDTH, DISPLAY_HEIGHT, FRAME_SIZE

UDP_TIMEOUT = 3.0

class COLOR(Structure):
		_fields_ = [('r', c_uint8), ('g', c_uint8), ('b', c_uint8), ('a', c_uint8)]

class FRAMEBUFFER(Structure):
		_fields_ = [('data', POINTER(COLOR)), ('w', c_size_t), ('h', c_size_t)]

bdf = CDLL('./libbdf.so')
bdf.read_bdf_file.restype = c_void_p
bdf.framebuffer_render_text.restype = POINTER(FRAMEBUFFER)
bdf.framebuffer_render_text.argtypes= [c_char_p, c_void_p, c_void_p, c_size_t, c_size_t, c_size_t]

unifont = bdf.read_bdf_file('unifont.bdf')

def compute_text_bounds(text):
	assert unifont
	textbytes = bytes(str(text), 'UTF-8')
	textw, texth = c_size_t(0), c_size_t(0)
	res = bdf.framebuffer_get_text_bounds(textbytes, unifont, byref(textw), byref(texth))
	if res:
		raise ValueError('Invalid text')
	return textw.value, texth.value

def render_text(text, offset):
	cbuf = create_string_buffer(FRAME_SIZE*sizeof(COLOR))
	textbytes = bytes(str(text), 'UTF-8')
	res = bdf.framebuffer_render_text(textbytes, unifont, cbuf, DISPLAY_WIDTH, DISPLAY_HEIGHT, offset)
	if res:
		raise ValueError('Invalid text')
	return np.ctypeslib.as_array(cast(cbuf, POINTER(c_uint8)), shape=(DISPLAY_WIDTH, DISPLAY_HEIGHT, 4))

printlock = threading.Lock()

def printframe(fb):
	w,h,_ = fb.shape
	printlock.acquire()
	print('\0337\033[H', end='')
	print('Rendering frame @{}'.format(time()))
	bdf.console_render_buffer(fb.ctypes.data_as(POINTER(c_uint8)), w, h)
	#print('\033[0m\033[KCurrently rendering', current_entry.entrytype, 'from', current_entry.remote, ':', current_entry.text, '\0338', end='')
	printlock.release()

def log(*args):
	printlock.acquire()
	print(strftime('\x1B[93m[%m-%d %H:%M:%S]\x1B[0m'), ' '.join(str(arg) for arg in args), '\x1B[0m')
	printlock.release()

class TextRenderer:
	def __init__(self, text):
		self.text = text
		self.width, _ = compute_text_bounds(text)

	def __iter__(self):
		for i in range(-DISPLAY_WIDTH, self.width):
			yield render_text(self.text, i)

class MateLightUDPServer:
	def __init__(self, port=1337, ip=''):
		self.current_client = None
		self.last_timestamp = 0
		self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		self.socket.bind((ip, port))
		self.thread = threading.Thread(target = self.udp_receive)
		self.thread.daemon = True
		self.start = self.thread.start
		self.frame_condition = threading.Condition()
		self.frame = None
	
	def frame_da(self):
		return self.frame is not None

	def __iter__(self):
		while True:
			with self.frame_condition:
				if not self.frame_condition.wait_for(self.frame_da, timeout=UDP_TIMEOUT):
					raise StopIteration()
				frame, self.frame = self.frame, None
			yield frame

	def udp_receive(self):
		while True:
			try:
				data, (addr, sport) = self.socket.recvfrom(FRAME_SIZE*3+4)
				timestamp = time()
				if timestamp - self.last_timestamp > UDP_TIMEOUT:
					self.current_client = addr
					log('\x1B[91mAccepting UDP data from\x1B[0m', addr)
				if addr == self.current_client:
					if len(data) == FRAME_SIZE*3+4:
						frame = data[:-4]
						crc1, = struct.unpack('!I', data[-4:])
						if crc1:
							crc2, = zlib.crc32(frame, 0),
							if crc1 != crc2:
								raise ValueError('Invalid frame CRC checksum: Expected {}, got {}'.format(crc2, crc1))
					elif len(data) == FRAME_SIZE*3:
						frame = data
					else:
						raise ValueError('Invalid frame size: {}'.format(len(data)))
					self.last_timestamp = timestamp
					with self.frame_condition:
						self.frame = np.frombuffer(frame, dtype=np.uint8).reshape((DISPLAY_WIDTH, DISPLAY_HEIGHT, 3))
						self.frame_condition.notify()
			except Exception as e:
				log('Error receiving UDP frame:', e)

renderqueue = deque()

class MateLightTCPTextHandler(BaseRequestHandler):
	def handle(self):
		global render_deque
		data = str(self.request.recv(1024).strip(), 'UTF-8')
		addr = self.client_address[0]
		if len(data) > 140:
			self.request.sendall('TOO MUCH INFORMATION!\n')
			return
		log('\x1B[95mText from\x1B[0m {}: {}\x1B[0m'.format(addr, data))
		renderqueue.append(TextRenderer(data))
		self.request.sendall(b'KTHXBYE!\n')

TCPServer.allow_reuse_address = True
tserver = TCPServer(('', 1337), MateLightTCPTextHandler)
t = threading.Thread(target=tserver.serve_forever)
t.daemon = True
t.start()

userver = MateLightUDPServer()
userver.start()

defaultlines = [ TextRenderer(l[:-1].replace('\\x1B', '\x1B')) for l in open('default.lines').readlines() ]
random.shuffle(defaultlines)
defaulttexts = itertools.cycle(itertools.chain(*defaultlines))

if __name__ == '__main__':
	#print('\033[?1049h'+'\n'*9)
	while True:
		if renderqueue:
			renderer = renderqueue.pop_front()
		elif userver.frame_da():
			renderer = userver
		else:
			sendframe(next(defaulttexts))
			continue
		for frame in renderer:
			sendframe(frame)
			#printframe(frame)

