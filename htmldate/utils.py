# pylint:disable-msg=E0611,I1101
"""
Module bundling functions related to HTML processing.
"""

## This file is available from https://github.com/adbar/htmldate
## under GNU GPL v3 license


# standard
import logging
import re

from typing import Any, List, Optional, Set, Union

import urllib3
import requests


# CChardet is faster and can be more accurate
try:
    from cchardet import detect as cchardet_detect  # type: ignore
except ImportError:
    cchardet_detect = None
from charset_normalizer import from_bytes

from lxml.html import HtmlElement, HTMLParser, fromstring  # type: ignore

from .settings import MAX_FILE_SIZE, MIN_FILE_SIZE


LOGGER = logging.getLogger(__name__)

UNICODE_ALIASES: Set[str] = {"utf-8", "utf_8"}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
RETRY_STRATEGY = urllib3.util.Retry(
    total=0,
    connect=0,
    status_forcelist=[429, 500, 502, 503, 504],
)
USER_AGENT = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36'}
HTTP_POOL = urllib3.PoolManager(
        retries=RETRY_STRATEGY,
        headers=USER_AGENT
)

HTML_PARSER = HTMLParser(
    collect_ids=False, default_doctype=False, encoding="utf-8", remove_pis=True
)

DOCTYPE_TAG = re.compile("^< ?! ?DOCTYPE.+?/ ?>", re.I)


def isutf8(data: bytes) -> bool:
    """Simple heuristic to determine if a bytestring uses standard unicode encoding"""
    try:
        data.decode("UTF-8")
    except UnicodeDecodeError:
        return False
    else:
        return True


def detect_encoding(bytesobject: bytes) -> List[str]:
    """Read all input or first chunk and return a list of encodings"""
    # alternatives: https://github.com/scrapy/w3lib/blob/master/w3lib/encoding.py
    # unicode-test
    if isutf8(bytesobject):
        return ["utf-8"]
    guesses = []
    # additional module
    if cchardet_detect is not None:
        cchardet_guess = cchardet_detect(bytesobject)["encoding"]
        if cchardet_guess is not None:
            guesses.append(cchardet_guess.lower())
    # try charset_normalizer on first part, fallback on full document
    detection_results = from_bytes(bytesobject[:15000]) or from_bytes(bytesobject)
    # return alternatives
    if len(detection_results) > 0:
        guesses.extend([r.encoding for r in detection_results])
    # it cannot be utf-8 (tested above)
    return [g for g in guesses if g not in UNICODE_ALIASES]


def decode_file(filecontent: Union[bytes, str]) -> str:
    """Guess bytestring encoding and try to decode to Unicode string.
    Resort to destructive conversion otherwise."""
    # init
    if isinstance(filecontent, str):
        return filecontent
    htmltext = None
    # encoding
    for guessed_encoding in detect_encoding(filecontent):
        try:
            htmltext = filecontent.decode(guessed_encoding)
        except (LookupError, UnicodeDecodeError):  # VISCII: lookup
            LOGGER.warning("wrong encoding detected: %s", guessed_encoding)
            htmltext = None
        else:
            break
    # return original content if nothing else succeeded
    return htmltext or str(filecontent, encoding="utf-8", errors="replace")


def decode_response(response: Any) -> str:
    """Read the urllib3 object corresponding to the server response, then
    try to guess its encoding and decode it to return a unicode string"""
    # urllib3 response object / bytes switch
    #if isinstance(response, urllib3.response.HTTPResponse) or hasattr(response, "data"):
    #    resp_content = response.data
    #else:
    resp_content = response
    return decode_file(resp_content)


def fetch_url(url: str) -> Optional[str]:
    """Fetches page using urllib3 and decodes the response.

    Args:
        url: URL of the page to fetch.

    Returns:
        HTML code as string, or Urllib3 response object (headers + body), or empty string in case
        the result is invalid, or None if there was a problem with the network.

    """
    # send
    try:
        # read by streaming chunks (stream=True, iter_content=xx)
        # so we can stop downloading as soon as MAX_FILE_SIZE is reached
#        response = HTTP_POOL.request("GET", url, timeout=2)  # type: ignore
        response = requests.get(url, headers=USER_AGENT, timeout=2)
    except Exception as err:
        LOGGER.error("download error: %s %s", url, err)  # sys.exc_info()[0]
    else:
        # safety checks
        if response.status_code != 200:
            LOGGER.error("not a 200 response: %s for URL %s", response.status_code, url)
        elif (
            response.content is None
            or len(response.content) < MIN_FILE_SIZE
            or len(response.content) > MAX_FILE_SIZE
        ):
            LOGGER.error("incorrect input data for URL %s", url)
        else:
            return decode_response(response.content)
    return None


def is_dubious_html(beginning: str) -> bool:
    "Assess if the object is proper HTML (awith a corresponding tag or declaration)."
    return "html" not in beginning


def strip_faulty_doctypes(htmlstring: str, beginning: str) -> str:
    "Repair faulty doctype strings to make then palatable for libxml2."
    # libxml2/LXML issue: https://bugs.launchpad.net/lxml/+bug/1955915
    if "doctype" in beginning:
        firstline, _, rest = htmlstring.partition("\n")
        return DOCTYPE_TAG.sub("", firstline, count=1) + "\n" + rest
    return htmlstring


def fromstring_bytes(htmlobject: str) -> Optional[HtmlElement]:
    "Try to pass bytes to LXML parser."
    tree = None
    try:
        tree = fromstring(htmlobject.encode("utf8"), parser=HTML_PARSER)
    except Exception as err:
        LOGGER.error("lxml parser bytestring %s", err)
    return tree


def load_html(htmlobject: Union[bytes, str, HtmlElement]) -> Optional[HtmlElement]:
    """Load object given as input and validate its type
    (accepted: lxml.html tree, bytestring and string)
    """
    # use tree directly
    if isinstance(htmlobject, HtmlElement):
        return htmlobject
    # do not accept any other type after this point
    if not isinstance(htmlobject, (bytes, str)):
        raise TypeError("incompatible input type: %s", type(htmlobject))
    # the string is a URL, download it
    if isinstance(htmlobject, str) and htmlobject.startswith("http"):
        htmltext = None
        if re.match(r"https?://[^ ]+$", htmlobject):
            LOGGER.info("URL detected, downloading: %s", htmlobject)
            htmltext = fetch_url(htmlobject)
            if htmltext is not None:
                htmlobject = htmltext
        # log the error and quit
        if htmltext is None:
            raise ValueError("URL couldn't be processed: %s", htmlobject)
    # start processing
    tree = None
    # try to guess encoding and decode file: if None then keep original
    htmlobject = decode_file(htmlobject)
    # sanity checks
    beginning = htmlobject[:50].lower()
    check_flag = is_dubious_html(beginning)
    # repair first
    htmlobject = strip_faulty_doctypes(htmlobject, beginning)
    # first pass: use Unicode string
    fallback_parse = False
    try:
        tree = fromstring(htmlobject, parser=HTML_PARSER)
    except ValueError:
        # "Unicode strings with encoding declaration are not supported."
        fallback_parse = True
        tree = fromstring_bytes(htmlobject)
    except Exception as err:  # pragma: no cover
        LOGGER.error("lxml parsing failed: %s", err)
    # second pass: try passing bytes to LXML
    if (tree is None or len(tree) < 2) and fallback_parse is False:
        tree = fromstring_bytes(htmlobject)
    # rejection test: is it (well-formed) HTML at all?
    # log parsing errors
    if tree is not None and check_flag is True and len(tree) < 2:
        LOGGER.error(
            "parsed tree length: %s, wrong data type or not valid HTML", len(tree)
        )
        tree = None
    return tree
