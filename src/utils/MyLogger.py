#!/usr/bin/python 
# -*- coding: UTF-8 -*-

from config import global_vars
import logging
from logging.config import dictConfig

import os
from os import path

class MyLogger:

    def init(self):
        if self.logger is None and "logger" in global_vars and global_vars["logger"] is not None:
            self.logger = global_vars["logger"]

    def __init__(self):
        self.logger = None
        self.init()

    def info(self,msg):
        self.init()
        print msg
        if self.logger is not None:
            self.logger.info(msg)

    def error(self,msg):
        self.init()
        print msg

        if self.logger is not None:
            self.logger.error(msg)

    def warn(self,msg):
        self.init()
        print msg

        if self.logger is not None:
            self.logger.warn(msg)

    def debug(self,msg):
        self.init()
        print msg

        if self.logger is not None:
            self.logger.debug(msg)


## 部署日志
class DeployLogger:

    def __init__(self):
        self.app_logger = None
        self.cmd_logger = None
        self.formatter  = None

        return

    def init(self, cmd=""):
        self.formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s', "%Y-%m-%d %H:%M:%S")

        ## 在当前目录下创建日志目录
        log_dir = "./logs/"
        if path.exists(log_dir) is False:
            os.mkdir(log_dir)

        cmd_dir = "./logs/cmd/"
        if path.exists(cmd_dir) is False and not cmd:
            os.mkdir(cmd_dir)

        self.app_logger = self.setup_logger("app", log_dir + "app.log")
        self.cmd_logger = self.setup_logger("cmd", cmd_dir + cmd + ".log")
        return

    def info(self,msg):

        if self.app_logger is not None:
            self.app_logger.info(msg)

        return

    def error(self,msg):

        if self.app_logger is not None:
            self.app_logger.error(msg)

        return

    def warn(self,msg):

        if self.app_logger is not None:
            self.app_logger.warn(msg)

        return

    def debug(self,msg):

        if self.app_logger is not None:
            self.app_logger.debug(msg)

        return

    def cmd(self, msg):

        if self.cmd_logger is not None:
            self.cmd_logger.info(msg)
        
        return

    def setup_logger(self, name, log_file, level=logging.INFO):
        handler = logging.FileHandler(log_file)        
        handler.setFormatter(self.formatter)

        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.addHandler(handler)

        return logger


deploy_logger = DeployLogger()
def get_deploy_logger():
    return deploy_logger

