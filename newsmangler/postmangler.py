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
import logging
import os
import select
import sys
import time

from cStringIO import StringIO

try:
    import xml.etree.cElementTree as ET
except:
    import xml.etree.ElementTree as ET

from newsmangler import asyncnntp
from newsmangler import yenc
from newsmangler.article import Article
from newsmangler.common import *

# ---------------------------------------------------------------------------

class PostMangler:
    def __init__(self, conf, debug=False):
        self.conf = conf
        
        self._conns = []
        self._idle = []
        
        # Create our logger
        self.logger = logging.getLogger('mangler')
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        if debug:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)
        
        # Create a poll object for async bits to use. If the user doesn't have
        # poll, we're going to have to fake it.
        try:
            asyncore.poller = select.poll()
        except AttributeError:
            from classes.FakePoll import FakePoll
            asyncore.poller = FakePoll()
        

        self.conf['posting']['skip_filenames'] = self.conf['posting'].get('skip_filenames', '').split()
        
        self._articles = []
        self._files = {}
        self._msgids = {}
        
        self._current_dir = None
        self.newsgroup = None
        self.post_title = None
        
        # Some sort of useful logging junk about what yEncode we're using
        self.logger.info('Using %s module for yEnc encoding.', yenc.yEncMode())
    
    # -----------------------------------------------------------------------
    # Connect all of our connections
    def connect(self):
        for i in range(self.conf['server']['connections']):
            conn = asyncnntp.asyncNNTP(self, i, self.conf['server']['hostname'],
                self.conf['server']['port'], None, self.conf['server']['username'],
                self.conf['server']['password'],
            )
            conn.do_connect()
            self._conns.append(conn)

    # -----------------------------------------------------------------------
    # Poll our poll() object and do whatever is neccessary. Basically a combination
    # of asyncore.poll2() and asyncore.readwrite(), without all the frippery.
    def poll(self):
        results = asyncore.poller.poll(0)
        for fd, flags in results:
            obj = asyncore.socket_map.get(fd)
            if obj is None:
                self.logger.warning('Invalid FD for poll() - %d', fd)
            
            try:
                if flags & (select.POLLIN | select.POLLPRI):
                    obj.handle_read_event()
                if flags & select.POLLOUT:
                    obj.handle_write_event()
                if flags & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                    obj.handle_expt_event()
            except asyncore.ExitNow:
                raise
            except:
                obj.handle_error()

    # -----------------------------------------------------------------------
    def post(self, newsgroup, postme, post_title=None):
        self.newsgroup = newsgroup
        self.post_title = post_title
        
        # Generate the list of articles we need to post
        self.generate_article_list(postme)
        
        # If we have no valid articles, bail
        if not self._articles:
            self.logger.warning('No valid articles to post!')
            return
        
        # Connect!
        self.connect()
        
        # Wait until at least one connection is ready
        last_stuff = time.time()
        while 1:
            now = time.time()
            self.poll()
            
            if self._idle:
                break
            
            if now - last_stuff >= 1:
                last_stuff = now
                for conn in self._conns:
                    if conn.state == asyncNNTP.STATE_DISCONNECTED and now >= conn.reconnect_at:
                        conn.do_connect()
            
            time.sleep(0.01)
        
        # And loop
        self._bytes = 0
        start = time.time()
        
        self.logger.info('Posting %d article(s)...', len(self._articles))
        
        while 1:
            now = time.time()
            
            # Poll our sockets for events
            self.poll()
            
            # Possibly post some more parts now
            while self._idle and self._articles:
                article = self._articles.pop(0)
                #postfile = StringIO()
                art = self.build_article(*article)
                
                conn = self._idle.pop(0)
                conn.post_article(art)
            
            # Do some stuff every ~0.5s
            if now - last_stuff >= 0.5:
                last_stuff = now
                
                for conn in self._conns:
                    if conn.state == asyncnntp.STATE_DISCONNECTED and now >= conn.reconnect_at:
                        conn.do_connect()
                
                if self._bytes:
                    interval = time.time() - start
                    speed = self._bytes / interval / 1024
                    left = len(self._articles) + (len(self._conns) - len(self._idle))
                    print '%d articles remaining - %.1fKB/s     \r' % (left, speed),
                    sys.stdout.flush()
            
            # All done?
            if len(self._articles) == 0 and len(self._idle) == self.conf['server']['connections']:
                interval = time.time() - start
                speed = self._bytes / interval / 1024
                self.logger.info('Posting complete - %d bytes in %.2fs (%.1fKB/s)',
                    self._bytes, interval, speed)
                
                # If we have some msgids left over, we might have to generate
                # a .NZB
                if self.conf['posting']['generate_nzbs'] and self._msgids:
                    self.generate_nzb()
                
                break
            
            # And sleep for a bit to try and cut CPU chompage
            time.sleep(0.01)
    
    # -----------------------------------------------------------------------
    # Maybe remember the msgid for later
    def remember_msgid(self, article_size, article):
        if self.conf['posting']['generate_nzbs']:
            if self._current_dir != article._fileinfo['dirname']:
                if self._msgids:
                    self.generate_nzb()
                    self._msgids = {}
                
                self._current_dir = article._fileinfo['dirname']
            
            subj = article._subject % (1)
            if subj not in self._msgids:
                self._msgids[subj] = [int(time.time())]
            self._msgids[subj].append((article.headers['Message-ID'], article_size))
    
    # -----------------------------------------------------------------------
    # Generate the list of articles we need to post
    def generate_article_list(self, postme):
        # "files" mode is just one lot of files
        if self.post_title:
            self._gal_files(self.post_title, postme)
        # "dirs" mode could be a whole bunch
        else:
            for dirname in postme:
                if dirname.endswith(os.sep):
                    dirname = dirname[:-len(os.sep)]
                if not dirname:
                    continue
                
                self._gal_files(os.path.basename(dirname), os.listdir(dirname), basepath=dirname)
        
        # Debug junk
        if 0:
            for article in self._articles:
                print article[1]
    
    # Do the heavy lifting for generate_article_list
    def _gal_files(self, post_title, files, basepath=''):
        article_size = self.conf['posting']['article_size']
        
        goodfiles = []
        for filename in files:
            filepath = os.path.abspath(os.path.join(basepath, filename))
            
            # Skip non-files and empty files
            if not os.path.isfile(filepath):
                continue
            if filename in self.conf['posting']['skip_filenames']:
                continue
            filesize = os.path.getsize(filepath)
            if not filesize:
                continue
            
            goodfiles.append((filename, filepath, filesize))
        goodfiles.sort()
        
        n = 1
        for filename, filepath, filesize in goodfiles:
            full, partial = divmod(filesize, article_size)
            if partial:
                parts = full + 1
            else:
                parts = full
            
            # Build a subject
            real_filename = os.path.split(filename)[1]
            
            temp = '%%0%sd' % (len(str(len(files))))
            filenum = temp % (n)
            temp = '%%0%sd' % (len(str(parts)))
            subject = '%s [%s/%d] - "%s" yEnc (%s/%d)' % (
                post_title, filenum, len(goodfiles), real_filename, temp, parts
            )
            
            if self.conf['posting']['subject_prefix']:
                subject = '%s %s' % (self.conf['posting']['subject_prefix'], subject)
            
            # Now make up our parts
            fileinfo = {
                'dirname': post_title,
                'filename': real_filename,
                'filepath': filepath,
                'filesize': filesize,
                'parts': parts,
            }
            
            for i in range(parts):
                self._articles.append([fileinfo, subject, i+1])
            
            n += 1
    
    # -----------------------------------------------------------------------
    # Build an article for posting.
    def build_article(self, fileinfo, subject, partnum):
        #(fileinfo, subject, partnum) = article
        
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
        
        # Make a new article object and set headers
        art = Article(data, begin, end, fileinfo, subject, partnum)
        art.headers['From'] = self.conf['posting']['from']
        art.headers['Newsgroups'] = self.newsgroup
        art.headers['Subject'] = subject % (partnum)
        art.headers['Message-ID'] = '<%.5f.%d@%s>' % (time.time(), partnum, self.conf['server']['hostname'])
        art.headers['X-Newsposter'] = 'newsmangler %s (%s) - https://github.com/madcowfred/newsmangler\r\n' % (
            NM_VERSION, yenc.yEncMode())

        return art
    
    # -----------------------------------------------------------------------
    # Generate a .NZB file!
    def generate_nzb(self):
        filename = 'newsmangler_%s.nzb' % (SafeFilename(self._current_dir))

        self.logger.info('Begin generation of %s', filename)

        gentime = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        root = ET.Element('nzb')
        root.append(ET.Comment('Generated by newsmangler v%s at %s' % (NM_VERSION, gentime)))

        for subject, msgids in self._msgids.items():
            posttime = msgids.pop(0)

            # file
            f = ET.SubElement(root, 'file',
                {
                    'poster': self.conf['posting']['from'],
                    'date': str(posttime),
                    'subject': subject,
                }
            )

            # newsgroups
            groups = ET.SubElement(f, 'groups')
            for newsgroup in self.newsgroup.split(','):
                group = ET.SubElement(groups, 'group')
                group.text = newsgroup

            # segments
            segments = ET.SubElement(f, 'segments')
            for i, (msgid, article_size) in enumerate(msgids):
                segment = ET.SubElement(segments, 'segment',
                    {
                        'bytes': str(article_size),
                        'number': str(i + 1),
                    }
                )
                segment.text = str(msgid)

        with open(filename, 'w') as nzbfile:
            ET.ElementTree(root).write(nzbfile, xml_declaration=True)

        self.logger.info('End generation of %s', filename)

# ---------------------------------------------------------------------------