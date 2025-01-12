# -*- coding: utf-8 -*-
#
# Copyright (C) 2011  Tiger Soldier
#
# This file is part of OSD Lyrics.
#
# OSD Lyrics is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OSD Lyrics is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OSD Lyrics.  If not, see <https://www.gnu.org/licenses/>.
#

from builtins import super
from future import standard_library
standard_library.install_aliases()

import logging
import os
import os.path
import re
import urllib.parse
import urllib.request

import chardet
import dbus
import dbus.service

import osdlyrics
from osdlyrics.app import App
import osdlyrics.config
import osdlyrics.lrc
from osdlyrics.metadata import Metadata
from osdlyrics.pattern import expand_file, expand_path

import lrcdb

LYRICS_INTERFACE = 'org.osdlyrics.Lyrics'
LYRICS_OBJECT_PATH = '/org/osdlyrics/Lyrics'

DEFAULT_FILE_PATTERNS = [
    '%p-%t',
    '%t-%p',
    '%f',
    '%t',
]

DEFAULT_PATH_PATTERNS = [
    '~/.lyrics',
    '%',
]

SUPPORTED_SCHEMES = [
    'file',
    # 'tag',
    'none',
]

DETECT_CHARSET_GUESS_MIN_LEN = 40
DETECT_CHARSET_GUESS_MAX_LEN = 100


class InvalidUriException(Exception):
    """ Exception of invalid uri.
    """

    def __init__(self, uri):
        super().__init__("Invalid URI: %s" % uri)


class CannotLoadLrcException(Exception):
    def __init__(self, uri):
        super().__init__("Cannot load lrc file from %s" % uri)


class CannotSaveLrcException(Exception):
    def __init__(self, uri):
        super().__init__("Cannot save lrc file to %s" % uri)


def metadata_description(metadata):
    if metadata.title is None:
        return '[Unknown]'
    if metadata.artist is None:
        return metadata.title
    return '%s(%s)' % (metadata.title, metadata.artist)


def decode_by_charset(content):
    # type: (bytes) -> Text
    r"""
    Detect the charset encoding of a string and decodes to unicode strings.

    >>> decode_by_charset(u'\u4e2d\u6587'.encode('UTF-8'))
    '\u4e2d\u6587'
    >>> decode_by_charset(u'\u4e2d\u6587'.encode('HZ-GB-2312'))
    '\u4e2d\u6587'
    """
    encoding = chardet.detect(content)['encoding']
    # Sometimes, the content is well encoded but the last few bytes. This is
    # common in the files downloaded by old versions of OSD Lyrics. In this
    # case,chardet may fail to determine what the encoding it is. So we take
    # half of the content of it and try again.
    if not encoding and len(content) > DETECT_CHARSET_GUESS_MIN_LEN:
        logging.warning('Failed to detect encoding, try to decode a part of it')
        content_half = len(content) // 2
        slice_end = min(max(DETECT_CHARSET_GUESS_MIN_LEN, content_half), DETECT_CHARSET_GUESS_MAX_LEN)
        encoding = chardet.detect(content[:slice_end])['encoding']
        logging.warning('guess encoding from part: ' + encoding)
    if not encoding:
        logging.warning('Failed to detect encoding, use utf-8 as fallback')
        encoding = 'utf-8'

    encoding = encoding.lower()
    # When we take half of the content to determine the encoding, chardet may
    # think it be encoded with ascii, however the full content is probably
    # encoded with utf-8. As ascii is an subset of utf-8, decoding an ascii
    # string with utf-8 will always be right.
    if encoding == 'ascii':
        encoding = 'utf-8'
    # Upgrade the Chinese encodings to their extended sets.
    elif encoding in ('gb2312', 'gbk'):
        encoding = 'gb18030'
    elif encoding == 'big5':
        encoding = 'big5hkscs'
    return content.decode(encoding, 'replace')


def is_valid_uri(uri):
    """
    Tell if a URI is valid.

    A valid URI must begin with the schemes defined in SUPPORTED_SCHEMES
    """
    for scheme in SUPPORTED_SCHEMES:
        if uri.startswith(scheme + ':'):
            return True
    return False


def ensure_uri_scheme(uri):
    # type: (Text) -> Text
    """
    Converts a file path to an URI with scheme of "file:", leaving other URI not
    changed

    If the uri doesn't have any scheme, it is considered to be a file path.
    """
    if uri:
        url_parts = urllib.parse.urlparse(uri)
        if not url_parts.scheme:
            uri = osdlyrics.utils.path2uri(uri)
    return uri


def _load_from_file(urlparts):
    """
    Load the content of file from urlparse.ParseResult

    Return the content of the file, or None if error raised.
    """
    path = urllib.request.url2pathname(urlparts.path)
    try:
        with open(path, 'rb') as f:
            return f.read()
    except IOError as e:
        logging.info("Cannot open file %s to read: %s", path, e)
        return None


def load_from_uri(uri):
    # type: (Text) -> Optional[Text]
    """
    Load the content of LRC file from given URI

    If loaded, return the content. If failed, return None.
    """
    URI_LOAD_HANDLERS = {
        'file': _load_from_file,
        'none': lambda uri: b'',
    }

    url_parts = urllib.parse.urlparse(uri)
    content = URI_LOAD_HANDLERS[url_parts.scheme](url_parts)
    if content is None:
        return None
    content = decode_by_charset(content).replace('\0', '')
    return content


def _save_to_file(urlparts, content, create):
    # type: (Any, bytes, bool) -> bool
    """
    Save the content of file to urlparse.ParseResult

    Return True if succeeded
    """
    path = urllib.request.url2pathname(urlparts.path)
    if not create:
        if not os.path.isfile(path):
            logging.warning("Cannot write to file %s: file not exists", path)
            return False
    else:
        dirname = os.path.dirname(path)
        if not os.path.isdir(dirname):
            try:
                os.makedirs(os.path.dirname(path), 0o755)
            except OSError as e:
                logging.warning("Cannot create directories for %s: %s", path, e)
                return False
    try:
        file = open(path, 'wb')
    except IOError as e:
        logging.info("Cannot open file %s to write: %s", path, e)
        return False
    file.write(content)
    return True


def save_to_uri(uri, content, create=True):
    # type: (Text, bytes, bool) -> bool
    """
    Save the content of LRC file to given URI.

    Return True if succeeded, or False if failed.
    """
    URI_SAVE_HANDLERS = {
        'file': _save_to_file,
        'none': lambda urlparts, content, create: True,
    }

    url_parts = urllib.parse.urlparse(uri)
    return URI_SAVE_HANDLERS[url_parts.scheme](url_parts, content, create)


def update_lrc_offset(content, offset):
    r"""
    Replace the offset attributes in the content of LRC file.
    >>> update_lrc_offset('no tag', 100)
    '[offset:100]\nno tag'
    >>> update_lrc_offset('[ti:title]\n[offset:200]\nSome lrc', 100)
    '[ti:title]\n[offset:100]\nSome lrc'
    >>> update_lrc_offset('[ti:title][offset:200]Some lrc\nanother', 100)
    '[ti:title][offset:100]Some lrc\nanother'
    >>> update_lrc_offset('Some [offset:200] lrc', 100)
    '[offset:100]\nSome [offset:200] lrc'
    >>> update_lrc_offset('[[offset:200]] lrc', 100)
    '[offset:100]\n[[offset:200]] lrc'
    """
    search_result = re.search(r'^(\[[^\]]*\])*?\[offset:(.*?)\]',
                              content,
                              re.MULTILINE)
    if search_result is None:
        return '[offset:%s]\n%s' % (offset, content)
    return '%s%s%s' % (content[:search_result.start(2)],
                       offset,
                       content[search_result.end(2):])


class LyricsService(dbus.service.Object):

    def __init__(self, conn):
        super().__init__(conn=conn, object_path=LYRICS_OBJECT_PATH)
        self._db = lrcdb.LrcDb()
        self._config = osdlyrics.config.Config(conn)
        self._metadata = Metadata()

    def find_lrc_from_db(self, metadata):
        uri = self._db.find(metadata)
        if uri == '':
            return 'none:'
        return ensure_uri_scheme(uri)

    def find_lrc_by_pattern(self, metadata):
        return ensure_uri_scheme(self._expand_patterns(metadata))

    def assign_lrc_uri(self, metadata, uri):
        self._db.assign(metadata, uri)
        if metadata == self._metadata:
            self.CurrentLyricsChanged()

    @dbus.service.method(dbus_interface=LYRICS_INTERFACE,
                         in_signature='a{sv}',
                         out_signature='bsa{ss}aa{sv}')
    def GetLyrics(self, metadata):
        ret, uri, content = self.GetRawLyrics(metadata)
        if ret:
            attr, lines = osdlyrics.lrc.parse_lrc(content)
            return ret, uri, attr, lines
        else:
            return ret, uri, {}, []

    @dbus.service.method(dbus_interface=LYRICS_INTERFACE,
                         in_signature='a{sv}',
                         out_signature='bss')
    def GetRawLyrics(self, metadata):
        if isinstance(metadata, dict):
            metadata = Metadata.from_dict(metadata)
        uri = self.find_lrc_from_db(metadata)
        lrc = None
        if uri:
            if uri == 'none:':
                return True, uri, ''
            lrc = load_from_uri(uri)
            if lrc is not None:
                return True, uri, lrc
        uri = self.find_lrc_by_pattern(metadata)
        if uri:
            lrc = load_from_uri(uri)
            if lrc is not None:
                logging.info("LRC for track %s not found in db but found by pattern: %s", metadata_description(metadata), uri)
        if lrc is None:
            logging.info("LRC for track %s not found", metadata_description(metadata))
            return False, '', ''
        else:
            logging.info("LRC for track %s found: %s", metadata_description(metadata), uri)
            return True, uri, lrc

    @dbus.service.method(dbus_interface=LYRICS_INTERFACE,
                         in_signature='',
                         out_signature='bsa{ss}aa{sv}')
    def GetCurrentLyrics(self):
        return self.GetLyrics(self._metadata)

    @dbus.service.method(dbus_interface=LYRICS_INTERFACE,
                         in_signature='',
                         out_signature='bss')
    def GetCurrentRawLyrics(self):
        return self.GetRawLyrics(self._metadata)

    @dbus.service.method(dbus_interface=LYRICS_INTERFACE,
                         in_signature='a{sv}ay',
                         out_signature='s',
                         byte_arrays=True)
    def SetLyricContent(self, metadata, content):
        metadata = Metadata.from_dict(metadata)
        # Remove any existing file association and save the new lyrics content
        # to the configured patterns.
        self._db.delete(metadata)
        uri = self._save_to_patterns(metadata, content.rstrip(b'\0'))
        if uri and metadata == self._metadata:
            self.CurrentLyricsChanged()
        return uri

    @dbus.service.method(dbus_interface=LYRICS_INTERFACE,
                         in_signature='a{sv}s',
                         out_signature='')
    def AssignLyricFile(self, metadata, uri):
        metadata = Metadata.from_dict(metadata)
        self.assign_lrc_uri(metadata, uri)

    @dbus.service.signal(dbus_interface=LYRICS_INTERFACE,
                         signature='')
    def CurrentLyricsChanged(self):
        pass

    @dbus.service.method(dbus_interface=LYRICS_INTERFACE,
                         in_signature='si',
                         out_signature='')
    def SetOffset(self, uri, offset_ms):
        if not is_valid_uri(uri):
            raise InvalidUriException(uri)
        content = load_from_uri(uri)
        if content is None:
            raise CannotLoadLrcException(uri)
        content = update_lrc_offset(content, offset_ms).encode('utf-8')
        if not save_to_uri(uri, content, True):
            raise CannotSaveLrcException(uri)

    def _save_to_patterns(self, metadata, content):
        """ Save content to file expanded from given patterns

        Arguments:
        - `metadata`:
        - `content`:
        """
        file_patterns = self._config.get_string_list('General/lrc-filename',
                                                     DEFAULT_FILE_PATTERNS)
        path_patterns = self._config.get_string_list('General/lrc-path',
                                                     DEFAULT_PATH_PATTERNS)
        for path_pat in path_patterns:
            try:
                path = expand_path(path_pat, metadata)
            except osdlyrics.pattern.PatternException:
                continue
            for file_pat in file_patterns:
                try:
                    filename = expand_file(file_pat, metadata)
                except osdlyrics.pattern.PatternException:
                    continue
                fullpath = os.path.join(path, filename + '.lrc')
                uri = osdlyrics.utils.path2uri(fullpath)
                if save_to_uri(uri, content):
                    return uri
        return ''

    def _expand_patterns(self, metadata):
        file_patterns = self._config.get_string_list('General/lrc-filename',
                                                     DEFAULT_FILE_PATTERNS)
        path_patterns = self._config.get_string_list('General/lrc-path',
                                                     DEFAULT_PATH_PATTERNS)
        for path_pat in path_patterns:
            try:
                path = expand_path(path_pat, metadata)
            except osdlyrics.pattern.PatternException:
                continue
            for file_pat in file_patterns:
                try:
                    filename = expand_file(file_pat, metadata)
                except osdlyrics.pattern.PatternException:
                    continue
                fullpath = os.path.join(path, filename + '.lrc')
                if os.path.isfile(fullpath):
                    return fullpath
        return None

    def set_current_metadata(self, metadata):
        logging.info('Setting current metadata: %s', metadata)
        self._metadata = metadata


def doc_test():
    import doctest
    doctest.testmod()


def test():
    app = App('Lyrics', False)
    lyrics_service = LyricsService(app.connection)  # noqa: F841
    app.run()


if __name__ == '__main__':
    doc_test()
    test()
