# ---------------------------------------------------------------------------
# $Id: poster.py 3875 2005-10-03 08:19:19Z freddie $
# ---------------------------------------------------------------------------
# Copyright (c) 2005, freddie@madcowdisease.org
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Main class for posting stuff."""

import asyncore
import os
import select
import sys
import time

from cStringIO import StringIO

from classes import asyncNNTP
from classes import yEnc
from classes.BaseMangler import BaseMangler
from classes.Common import *

__version__ = '0.01'

# ---------------------------------------------------------------------------

class Poster(BaseMangler):
	def __init__(self, conf):
		BaseMangler.__init__(self, conf)
		
		self.conf['posting']['skip_filenames'] = self.conf['posting'].get('skip_filenames', '').split()
		
		self._articles = []
		self._files = {}
		
		self._current_dir = None
		self._msgids = {}
		self.newsgroup = None
	
	def post(self, newsgroup, dirs):
		self.newsgroup = newsgroup
		
		# Generate the list of articles we need to post
		self.generate_article_list(dirs)
		
		# connect!
		self.connect()
		
		# Slight speedup
		_poll = self.poll
		_sleep = time.sleep
		_time = time.time
		
		# Wait until at least one connection is ready
		counter = 0
		while 1:
			now = _time()
			_poll()
			
			if self._idle:
				break
			
			counter += 1
			if counter == 100:
				counter = 0
				for conn in self._conns:
					if conn.state == asyncNNTP.STATE_DISCONNECTED and now >= conn.reconnect_at:
						conn.do_connect()
			
			_sleep(0.01)
		
		# And loop
		self._bytes = 0
		start = _time()
		
		self.logger.info('Posting %d articles...', len(self._articles))
		
		while 1:
			now = _time()
			
			# Poll our sockets for events
			_poll()
			
			# Possibly post some more parts now
			while self._idle and self._articles:
				article = self._articles.pop(0)
				postfile = StringIO()
				self.build_article(postfile, article)
				
				conn = self._idle.pop(0)
				conn.post_article(postfile)
			
			# Do some stuff roughly once per second
			counter += 1
			if counter = 100:
				counter = 0
				
				for conn in self._conns:
					if conn.state == asyncNNTP.STATE_DISCONNECTED and now >= conn.reconnect_at:
						conn.do_connect()
				
				if self._bytes:
					interval = time.time() - start
					speed = self._bytes / interval / 1024
					print '%d articles remaining - %.1fKB/s     \r' % (len(self._articles), speed)
					sys.stdout.flush()
			
			# All done?
			if self._articles == [] and len(self._idle) == self.conf['server']['connections']:
				interval = time.time() - start
				speed = self._bytes / interval / 1024
				self.logger.info('Posting complete - %d bytes in %.2fs (%.1fKB/s)',
					self._bytes, interval, speed)
				
				# If we have some msgids left over, we might have to generate
				# a .NZB
				if self.conf['posting']['generate_nzbs'] and self._msgids:
					self.generate_nzb()
				
				return
			
			# And sleep for a bit to try and cut CPU chompage
			_sleep(0.01)
	
	# -----------------------------------------------------------------------
	# Generate the list of articles we need to post
	def generate_article_list(self, dirs):
		for dirname in dirs:
			if dirname.endswith(os.sep):
				dirname = dirname[:-len(os.sep)]
			if not dirname:
				continue
			
			article_size = self.conf['posting']['article_size']
			
			# Get a list of useful files
			f = os.listdir(dirname)
			files = []
			for filename in f:
				filepath = os.path.join(dirname, filename)
				# Skip non-files and empty files
				if not os.path.isfile(filepath):
					continue
				if not os.path.getsize(filepath):
					continue
				if filename in self.conf['posting']['skip_filenames']:
					continue
				files.append(filename)
			files.sort()
			
			n = 1
			for filename in files:
				filepath = os.path.join(dirname, filename)
				filesize = os.path.getsize(filepath)
				
				full, partial = divmod(filesize, article_size)
				if partial:
					parts = full + 1
				else:
					parts = full
				
				# Build a subject
				temp = '%%0%sd' % (len(str(len(files))))
				filenum = temp % (n)
				temp = '%%0%sd' % (len(str(parts)))
				subject = '%s [%s/%d] - "%s" yEnc (%s/%d)' % (
					os.path.basename(dirname), filenum, len(files), filename, temp, parts
				)
				
				if self.conf['posting']['subject_prefix']:
					subject = '%s %s' % (self.conf['posting']['subject_prefix'], subject)
				
				# Now make up our parts
				fileinfo = {
					'dirname': dirname,
					'filename': filename,
					'filepath': filepath,
					'filesize': filesize,
					'parts': parts,
				}
				
				for i in range(parts):
					article = [fileinfo, subject, i+1]
					self._articles.append(article)
				
				n += 1
	
	# -----------------------------------------------------------------------
	# Build an article for posting.
	def build_article(self, postfile, article):
		(fileinfo, subject, partnum) = article
		
		# Read the chunk of data from the file
		f = self._files.get(fileinfo['filepath'], None)
		if f is None:
			self._files[fileinfo['filepath']] = f = open(fileinfo['filepath'], 'rb')
		
		begin = f.tell()
		data = f.read(self.conf['posting']['article_size'])
		end = f.tell()
		
		# If that was the last part, close the file and throw it away
		if partnum == fileinfo['parts']:
			self._files[fileinfo['filepath']].close()
			del self._files[fileinfo['filepath']]
		
		# Basic headers
		line = 'From: %s\r\n' % (self.conf['posting']['from'])
		postfile.write(line)
		
		line = 'Newsgroups: %s\r\n' % (self.newsgroup)
		postfile.write(line)
		
		#line = time.strftime('Date: %a, %d %b %Y %H:%M:%S GMT\r\n', time.gmtime())
		#postfile.write(line)
		
		subj = subject % (partnum)
		line = 'Subject: %s\r\n' % (subj)
		postfile.write(line)
		
		msgid = '%.5f,%d@%s' % (time.time(), partnum, self.conf['server']['hostname'])
		line = 'Message-ID: <%s>\r\n' % (msgid)
		postfile.write(line)
		
		line = 'X-Newsposter: newsmangler %s - http://www.madcowdisease.org/mcd/newsmangler\r\n' % (__version__)
		postfile.write(line)
		
		postfile.write('\r\n')
		
		# yEnc start
		line = '=ybegin part=%d total=%d line=256 size=%d name=%s\r\n' % (
			partnum, fileinfo['parts'], fileinfo['filesize'], fileinfo['filename']
		)
		postfile.write(line)
		line = '=ypart begin=%d end=%d\r\n' % (begin+1, end)
		postfile.write(line)
		
		# yEnc data
		yEnc.yEncode(postfile, data)
		
		# yEnc end
		partcrc = CRC32(data)
		line = '=yend size=%d part=%d pcrc32=%s\r\n' % (end-begin, partnum, partcrc)
		postfile.write(line)
		
		# And done writing for now
		postfile.write('.\r\n')
		article_size = postfile.tell()
		postfile.seek(0, 0)
		
		# Maybe remember the msgid for later
		if self.conf['posting']['generate_nzbs']:
			if self._current_dir != fileinfo['dirname']:
				if self._msgids:
					self.generate_nzb()
					self._msgids = {}
				
				self._current_dir = fileinfo['dirname']
			
			subj = subject % (1)
			if subj not in self._msgids:
				self._msgids[subj] = [int(time.time())]
			self._msgids[subj].append((msgid, article_size))
	
	# -----------------------------------------------------------------------
	# Generate a .NZB file!
	def generate_nzb(self):
		filename = 'newsmangler_%s.nzb' % (SafeFilename(self._current_dir))
		nzbfile = open(filename, 'w')
		
		nzbfile.write('<?xml version="1.0" encoding="iso-8859-1" ?>\n')
		nzbfile.write('<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.0//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.0.dtd">\n')
		nzbfile.write('<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n')
		
		for subject, msgids in self._msgids.items():
			posttime = msgids.pop(0)
			
			line = '  <file poster="%s" date="%s" subject="%s">\n' % (
				XMLBrackets(self.conf['posting']['from']), posttime,
				XMLBrackets(subject)
			)
			nzbfile.write(line)
			nzbfile.write('    <groups>\n')
			
			for newsgroup in self.newsgroup.split(','):
				line = '      <group>%s</group>\n' % (newsgroup)
				nzbfile.write(line)
			
			nzbfile.write('    </groups>\n')
			nzbfile.write('    <segments>\n')
			
			for i, (msgid, article_size) in enumerate(msgids):
				line = '      <segment bytes="%s" number="%s">%s</segment>\n' % (
					article_size, i+1, msgid
				)
				nzbfile.write(line)
			
			nzbfile.write('    </segments>\n')
			nzbfile.write('  </file>\n')
		
		gentime = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
		line = '\n  <!-- Generated by newsmangler at %s -->\n' % (gentime)
		nzbfile.write(line)
		
		nzbfile.write('</nzb>\n')
		nzbfile.close()
		
		self.logger.info('Generated %s', filename)

# ---------------------------------------------------------------------------
