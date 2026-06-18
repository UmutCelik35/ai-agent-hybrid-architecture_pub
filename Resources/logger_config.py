import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Skip rollover when another Windows process has the active log locked."""

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            self.rolloverAt = self.computeRollover(int(time.time()))

class ColoredFormatter(logging.Formatter):
    """Custom formatter to add colors to console output using ANSI codes."""
    
    grey = "\x1b[38;20m"
    green = "\x1b[32;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    
    format_str = "%(asctime)s | %(levelname)s | %(module)s - %(message)s"

    # Hangi seviyeye hangi rengin verileceği sözlüğü
    FORMATS = {
        logging.DEBUG: grey + format_str + reset,
        logging.INFO: green + format_str + reset,
        logging.WARNING: yellow + format_str + reset,
        logging.ERROR: red + format_str + reset,
        logging.CRITICAL: bold_red + format_str + reset    
        # reset provides color reset after the message, so that only the log line is colored, not the rest of the terminal output.
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)
    

def get_logger(name="XrayAuto"):
    """
    Creates and configures a customized logger with both console and file handlers.
    """
    logger = logging.getLogger(name) 
    logger.setLevel(logging.DEBUG)

    if logger.hasHandlers(): # prevents adding multiple handlers if get_logger is called multiple times
        return logger

    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(module)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # --- 1. CONSOLE SETTINGS (Terminal Output) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) 
    console_handler.setFormatter(ColoredFormatter()) 

    # --- 2. FILE SETTINGS ---
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(exist_ok=True)
    
    log_filename = log_dir / "automation.log"

    # TimedRotatingFileHandler:
    # when="midnight": Gece yarısı böl.
    # interval=1: Her 1 günde bir.
    # backupCount=4: 1 aktif + 4 yedek = Toplam 5 dosya sakla. En eskisini sil.
    file_handler = WindowsSafeTimedRotatingFileHandler(
        filename=log_filename,
        when="midnight",
        interval=1,
        backupCount=4, 
        encoding="utf-8"
    )
    
    # show date in filename for easier debugging and log management
    file_handler.suffix = "%Y-%m-%d"
    
    # Every log level should be saved to the file for complete records, so we set it to DEBUG.
    file_handler.setLevel(logging.DEBUG) 

    file_handler.setFormatter(file_formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger
