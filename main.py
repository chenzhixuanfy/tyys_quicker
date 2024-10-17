import datetime
import os
import random
import sys
import ddddocr
import base64
import traceback
import requests
import json
import re
import time
import hashlib
import argparse
import logging
import cv2
import copy

import numpy as np

from io import BytesIO
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, JobExecutionEvent
from dateutil import tz
from Crypto.Cipher import AES

parser = argparse.ArgumentParser()
parser.add_argument('-i', '--input', type=str, default='./in.txt', metavar='',
                    help='场馆预约信息文件，默认为 ./in.txt')
parser.add_argument('-m', '--mode', choices=['interval', 'once', 'debug'], default='interval',  metavar='',
                    help="选择模式：interval, once, debug")
parser.add_argument('-t', '--start_time',  metavar='',
                    default=datetime.datetime.now(tz.gettz('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S"), 
                    help='选择脚本运行时间，格式形如"2024-04-04 17:15:30"。默认为立刻执行'
                    )
parser.add_argument('-b', '--buddy', default=-1, metavar='',
                    help='输入同伴码。默认自动获取同伴码，需要同伴通行证')
args = parser.parse_args()
schedule = BlockingScheduler()
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
handlers = [logging.StreamHandler()]
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)
logger = logging.getLogger()


class Reserver:
    def __init__(self):
        with open(args.input, 'r', encoding='UTF-8') as f:
            # 请输入场地编号(玉泉羽毛球场(39)):
            self.venue_site_id = f.readline().replace('\n', '')
            # 请输入预约日期(yyyy-mm-dd):
            self.date = f.readline().replace('\n', '')
            # 请输入候选开始时间(hh:mm)，空格隔开:
            candidate_str = f.readline().replace('\n', '')
            self.candidate = [self.date + " " + i for i in candidate_str.split(" ")]
            # 请输入优先预约的场地号:
            self.space_id = f.readline().replace('\n', '')
            # 请输入同伴姓名（必须一起预约过）:
            companion_str = f.readline().replace('\n', '')
            self.companion = companion_str.split(" ")
            # 请输入手机号:
            self.phone = f.readline().replace('\n', '')

        logger.info("-----------------")
        logger.info("场地编号: " + str(self.venue_site_id))
        logger.info("预约日期: " + str(self.date))
        logger.info("候选时间: " + str(self.candidate))
        logger.info("场地号: " + str(self.space_id))
        logger.info("同伴: " + str(self.companion))
        logger.info("手机号: " + self.phone)
        logger.info("-----------------\n")


class User(object):
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.login_url = "https://zjuam.zju.edu.cn/cas/login?service=http://www.tyys.zju.edu.cn/venue-server/sso/manageLogin"
        self.info_url = "http://www.tyys.zju.edu.cn/venue-server/api/reservation/day/info"
        self.order_url = "http://www.tyys.zju.edu.cn/venue-server/api/reservation/order/info"
        self.submit_url = "http://www.tyys.zju.edu.cn/venue-server/api/reservation/order/submit"
        self.pay_url = "http://www.tyys.zju.edu.cn/venue-server/api/venue/finances/order/pay"
        self.buddy_no_url = "http://www.tyys.zju.edu.cn/venue-server/api/vip/view/buddy_no"
        self.get_captcha_url = "http://www.tyys.zju.edu.cn/venue-server/api/captcha/get"
        self.check_captcha_url = "http://www.tyys.zju.edu.cn/venue-server/api/captcha/check"
        self.sess = requests.Session()
        self.sign = ""
        self.access_token = ""
        self.deny_list = []
        self.local_storage = {}

    def login(self):
        """Login to ZJU platform"""
        res = self.sess.get(self.login_url)
        try:
            execution = re.search(
                'name="execution" value="(.*?)"', res.text).group(1)
        except BaseException as exception:
            logger.critical(res.text)
            raise exception
        res = self.sess.get(
            url='https://zjuam.zju.edu.cn/cas/v2/getPubKey').json()
        n, e = res['modulus'], res['exponent']
        encrypt_password = self._rsa_encrypt(self.password, e, n)

        data = {
            'username': self.username,
            'password': encrypt_password,
            'authcode': '',
            'execution': execution,
            '_eventId': 'submit'
        }
        # 使用allow_redirects=False关闭自动重定向，否则状态码是200而不是302，并且响应头部中找不到
        # "Location": "http://www.tyys.zju.edu.cn/venue-server/sso/manageLogin?ticket=ST-1502458-5r7KVsfGQSALcrEUKtKm-zju.edu.cn"
        # res = self.sess.post(url=self.login_url, data=data)
        res = self.sess.post(url=self.login_url, data=data, allow_redirects=False)

        # # check if login successfully
        # if '统一身份认证' in res.content.decode():
        #     raise LoginError('登录失败，请核实账号密码重新登录')
        
        # logger.info(res.status_code)
        # for header, value in res.headers.items():
        #     logger.info(f"{header}: {value}")
        url_ST = res.headers["Location"] # 获取重定向url
        # logger.info(url)

        res = self.sess.post(url_ST, allow_redirects=False) # 使用allow_redirects=False关闭自动重定向
        # logger.info(res.headers)
        url_jsessionid = res.headers["Location"] # 获取重定向url

        res = self.sess.post(url_jsessionid, allow_redirects=False) # 使用allow_redirects=False关闭自动重定向
        # logger.info(res.headers)
        url_oauth_token = res.headers["Location"] # 获取重定向url
        # logger.info(url_oauth_token)

        # 匹配oauth_token=后面的部分的正则表达式
        pattern = r'oauth_token=([^&]+)'
        # 查找所有匹配项
        matches = re.search(pattern, url_oauth_token)
        # 输出匹配到的部分
        if matches:
            oauth_token = matches.group(1)
            logger.info(f"OAuth Token Value: {oauth_token}")
        else:
            logger.info("No match found.")

        res = self.sess.get(url_oauth_token, allow_redirects=False)

        timestamp = self.get_timestamp()
        self.sign = self.get_sign(path="/api/login", timestamp=timestamp, params={})
        sso_token = self.sess.cookies.get("sso_zju_tyb_token")
        
        res = self.sess.post("http://www.tyys.zju.edu.cn/venue-server/api/login",
                             headers={
                                 "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                                 "accept": "application/json, text/plain, */*",
                                 "accept-language": "zh-CN,zh;q=0.9",
                                 "app-key": "8fceb735082b5a529312040b58ea780b",
                                 "content-type": "application/x-www-form-urlencoded",
                                 "sign": self.sign,
                                 "sso-token": sso_token,
                                 "oauth-token": oauth_token,
                                 "timestamp": timestamp
                             })
                            
        # logger.info(res.content.decode())
        self.access_token = res.json()["data"]["token"]["access_token"]

        timestamp = self.get_timestamp()
        self.sign = self.get_sign(path="/roleLogin", timestamp=timestamp, params={})
        res = self.sess.post("http://www.tyys.zju.edu.cn/venue-server/roleLogin",
                             headers={
                                 "accept": "application/json, text/plain, */*",
                                 "accept-language": "zh-CN,zh;q=0.9",
                                 "app-key": "8fceb735082b5a529312040b58ea780b",
                                 "cgauthorization": self.access_token,
                                 "content-type": "application/x-www-form-urlencoded",
                                 "sign": self.sign,
                                 "timestamp": timestamp
                             },
                             params={
                                 "roleid": "3"
                             })
        self.access_token = res.json()["data"]["token"]["access_token"]
        if self.access_token != "":
            logger.info(self.username + " Login Success!")
        return self.sess

    def _rsa_encrypt(self, password_str, e_str, M_str):
        password_bytes = bytes(password_str, 'ascii')
        password_int = int.from_bytes(password_bytes, 'big')
        e_int = int(e_str, 16)
        M_int = int(M_str, 16)
        result_int = pow(password_int, e_int, M_int)
        return hex(result_int)[2:].rjust(128, '0')

    def get_info(self, venue_site_id, search_date):
        timestamp = self.get_timestamp()
        params = {
            "nocache": timestamp,
            "venueSiteId": venue_site_id,
            "searchDate": search_date,
        }
        url = self.info_url + "?venueSiteId=" + str(
            venue_site_id) + "&searchDate=" + search_date + "&nocache=" + timestamp

        self.sign = self.get_sign(timestamp=timestamp, params=params, path="/api/reservation/day/info")
        res = self.sess.get(url, headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "app-key": "8fceb735082b5a529312040b58ea780b",
            "cgauthorization": self.access_token,
            "content-type": "application/x-www-form-urlencoded",
            "sign": self.sign,
            "timestamp": timestamp
        })
        return res.json()

    def get_sign(self, timestamp, path, params):
        I = "c640ca392cd45fb3a55b00a63a86c618"
        c = I + path
        for key, value in sorted(params.items()):
            c += key + str(value)
        c += timestamp + " " + I
        return hashlib.md5(c.encode(encoding='UTF-8')).hexdigest()

    @staticmethod
    def choose_space(info, reserver):
        N = len(info)
        space_id = int(reserver.space_id)
        # 这里的space_id实际上指的是info中的"spaceName"而不是"id"
        if space_id > N or space_id < 1: # 如果不在合理区间，则随机选择一个场地
            space_id = random.randint(1, N) # 生成1-N之间的随机整数，包括1和N

        info_sh = copy.deepcopy(info) # 如果不使用深度拷贝，将直接改变函数调用时的实参

        # 通过操作数组顺序，选择场地号
        target_space = info_sh.pop(space_id - 1) # 提取出目标场地
        random.shuffle(info_sh) # 打乱列表的其余位置
        info_sh.insert(0, target_space) # 将目标场地插入到首位

        for space in info_sh:
            for key, value in space.items():
                if key.isnumeric() and value["reservationStatus"] == 1 and value["startDate"] in reserver.candidate:
                    # if reserver.n_site == 2:
                    #     if str(int(key) + 1) in space.keys() and space[key + 1]["reservationStatus"] == 1:
                    #         return [{
                    #             "spaceId": str(space["id"]), "timeId": str(int(key) + 1), "venueSpaceGroupId": None
                    #         }, {
                    #             "spaceId": str(space["id"]), "timeId": str(key), "venueSpaceGroupId": None
                    #         }]
                    # else:
                    return [{
                        "spaceId": str(space["id"]), "timeId": str(key), "venueSpaceGroupId": None
                    }]
        return []

    def order(self, buddy_no, reserver):
        while True:
            response = self.get_info(reserver.venue_site_id, reserver.date)
            if str(response['code']) != '200':
                return response
            # logger.info(response)

            # info存储了这个场馆各个场地的信息，见info.json。每个场地为一个字典，其中"id"表示场地id（唯一，但是不连续），"spaceName"表示场地号。
            # "reservationStatus"是预约信息，1表示可预约，4表示已被预约。
            info = response["data"]["reservationDateSpaceInfo"][
                reserver.date]
            token = response["data"]["token"]

            # with open("info.json", "w", encoding="utf-8") as f:
            #     json.dump(info, f, ensure_ascii=False, indent=4)

            while True:
                order = self.choose_space(info, reserver) # 选择场馆

                if len(order) == 0:
                    logger.critical("所有场次均被预约")
                    return None

                timestamp = self.get_timestamp()
                order = str(order).replace(": ", ":").replace(", ", ",").replace("\'", "\"").replace("None", "null")
                params = {
                    "venueSiteId": reserver.venue_site_id,
                    "reservationDate": reserver.date,
                    "weekStartDate": reserver.date,
                    "reservationOrderJson": order,
                    "token": token,
                }
                self.sign = self.get_sign(path="/api/reservation/order/info", timestamp=timestamp, params=params)
                res = self.sess.post(self.order_url, headers={
                    "accept": "application/json, text/plain, */*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "app-key": "8fceb735082b5a529312040b58ea780b",
                    "cgauthorization": self.access_token,
                    "content-type": "application/x-www-form-urlencoded",
                    "sign": self.sign,
                    "timestamp": timestamp
                }, params=params).json()

                if res["code"] == 200:
                    logger.info(order)
                    break
            
            # with open("buddy_list.json", "w", encoding="utf-8") as f:
            #     json.dump(res, f, ensure_ascii=False, indent=4)

            buddy_list = res["data"]["buddyList"]

            buddy_ids = ""
            for buddy in sorted(buddy_list, key=lambda i: i['id']):
                if buddy["name"] in reserver.companion:
                    if len(buddy_ids) != 0:
                        buddy_ids += ","
                    buddy_ids += str(buddy["id"])

            captcha_verification = self.solve_captcha(mode='clickWord') # 识别验证码，返回的是一串加密后的字符串

            # logger.info(captcha_verification)

            # 防止：{'code': 250, 'message': '预约步骤流程耗时异常，订单提交失败', 'data': None}
            time.sleep(1)
            params = {
                "venueSiteId": reserver.venue_site_id,
                "reservationDate": reserver.date,
                "reservationOrderJson": order,
                "phone": int(reserver.phone),
                "buddyIds": buddy_ids,
                "weekStartDate": reserver.date,
                "isCheckBuddyNo": 1,
                "captchaVerification": captcha_verification,
                "buddyNo": buddy_no,
                "isOfflineTicket": 1,
                "token": token,
            }
            timestamp = self.get_timestamp()
            self.sign = self.get_sign(timestamp, "/api/reservation/order/submit", params)
            res = self.sess.post(self.submit_url, headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "app-key": "8fceb735082b5a529312040b58ea780b",
                "cgauthorization": self.access_token,
                "content-type": "application/x-www-form-urlencoded",
                "sign": self.sign,
                "timestamp": timestamp
            }, params=params).json()

            if res["code"] == 200:
                break
            else:
                logger.info(res)

        trade_no = res["data"]["orderInfo"]["tradeNo"]
        params = {
            "venueTradeNo": trade_no,
            "isApp": 0
        }
        timestamp = self.get_timestamp()
        self.sign = self.get_sign(timestamp, "/api/venue/finances/order/pay", params)
        res = self.sess.post(self.pay_url, headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "app-key": "8fceb735082b5a529312040b58ea780b",
            "cgauthorization": self.access_token,
            "content-type": "application/x-www-form-urlencoded",
            "sign": self.sign,
            "timestamp": timestamp
        }, params=params).json()

        return res

    def exec(self, buddy_no, reserver, mode):
        try:
            self.sess = requests.Session()
            self.login()
            result = self.order(buddy_no, reserver)
            if result is not None:
                logger.info(result)
                if result["code"] == 200:
                    logger.info('Success!')
                return result
            else:
                return {'code': '409'}

        except BaseException:
            logger.info(traceback.format_exc())
            return {'code': '400'}

    def get_timestamp(self):
        return str(int(round(time.time() * 1000)))

    def get_buddy_no(self):
        self.login()
        timestamp = self.get_timestamp()
        params = {}
        self.sign = self.get_sign(timestamp=timestamp, params=params, path="/api/vip/view/buddy_no")
        res = self.sess.post(self.buddy_no_url, headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "app-key": "8fceb735082b5a529312040b58ea780b",
            "cgauthorization": self.access_token,
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "sign": self.sign,
            "timestamp": timestamp
        })
        return res.json()['data']
    
    def Hanzi_filter(self, text):
        # 定义一个匹配汉字的正则表达式模式
        pattern = re.compile(r'[\u4e00-\u9fff]')
        
        # 使用 findall 方法查找字符串中所有的汉字
        chinese_characters = pattern.findall(text)
        
        # 根据不同情况返回结果
        if len(chinese_characters) == 0: # 如果输入字符串中不含有汉字，则返回0
            return 0
        elif len(chinese_characters) == 1 and len(text) == 1: # 如果输入字符串中只有一个汉字，则直接返回输入字符串（目前没有发现识别出2个汉字的情况）
            return text
        else: # 如果输入字符串中同时有汉字和非汉字，则返回剔除非汉字部分的字符串
            return ''.join(chinese_characters)

    # @staticmethod # 不需要这个修饰吧
    def ocr_captcha(self, base64_img, word_list):
        det = ddddocr.DdddOcr(det=True)
        ocr = ddddocr.DdddOcr(beta=True)

        img = base64.b64decode(base64_img)

        timestamp = self.get_timestamp()

        with open(f"ocr_save/{timestamp}.png", "wb") as image_file:
            image_file.write(img)

        if 0: # 是否进行灰度化预处理
            # 将二进制数据转换为NumPy数组
            nparr = np.frombuffer(img, np.uint8)
            image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            # 转换为灰度图像
            gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            # 将灰度化后的图像编码回二进制数据
            _, img = cv2.imencode('.jpg', gray_image)

        # 下面这两行貌似可以不需要
        stream = BytesIO(img)
        image_bytes = stream.read()

        poses = det.detection(image_bytes) # 使用 ddddocr 检测图像中字的位置，返回[x1, y1, x2, y2]，用以确定包含目标的一个矩形区域。

        # 将二进制图像数据转为张量，方便后面切片操作（im[y1:y2, x1:x2]）
        arr = np.frombuffer(img, np.uint8)
        im = cv2.imdecode(arr, cv2.IMREAD_COLOR)

        decode_dict = {}
        i = 0
        for box in poses:
            x1, y1, x2, y2 = box
            cropped_img = im[y1:y2, x1:x2]
            cv2.imwrite("cropped.jpg", cropped_img)
            with open("cropped.jpg", 'rb') as f:
                cropped_img = f.read()
            res = ocr.classification(cropped_img) # 识别[x1, y1, x2, y2]区域内的汉字

            res = self.Hanzi_filter(res)
            if res == 0: # 识别出来的不是汉字，也要，因为有时候就是没识别出汉字
                res = str(i) # 确保字典的key不重复，否则只会记录最后一个

            decode_dict[res] = {
                'x': int((x1 + x2) / 2),
                'y': int((y1 + y2) / 2),
            }

            i+=1

        with open(f"ocr_save/{timestamp}.json", "w", encoding="utf-8") as f:
            json.dump(decode_dict, f, ensure_ascii=False, indent=4)

        res = []
        for word in word_list:
            if word in decode_dict.keys():
                res.append(decode_dict[word])
            else:
                candidates = list(filter(lambda x: x not in word_list, decode_dict.keys())) # 从 decode_dict 的键中筛选出不在 word_list 中的键（即字），并返回这些键的列表
                # 碰运气
                res.append(decode_dict[random.choice(candidates)])
        return res

    def solve_click_word(self):
        if 'slider' not in self.local_storage.keys():
            # set uuid
            t = '0123456789abcdef'
            e = []
            for i in range(36):
                e.append(t[int(np.floor(16 * np.random.rand()))])
            e[14] = '4'
            e[19] = t[3 & int(e[19], 16) | 8]
            e[8] = e[13] = e[18] = e[23] = '-'
            self.local_storage['slider'] = 'slider-' + ''.join(e)
            self.local_storage['point'] = 'point-' + ''.join(e)
        client_uid = self.local_storage['point']

        timestamp = self.get_timestamp()

        url = f'{self.get_captcha_url}?captchaType=clickWord&clientUid={client_uid}&ts={timestamp}&nocache={timestamp}'
        params = {
            'captchaType': 'clickWord',
            'clientUid': client_uid,
            'ts': timestamp,
            'nocache': timestamp
        }
        self.sign = self.get_sign(timestamp=timestamp, params=params, path="/api/captcha/get")
        res = self.sess.get(url, headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "app-key": "8fceb735082b5a529312040b58ea780b",
            "cgauthorization": self.access_token,
            "content-type": "application/x-www-form-urlencoded",
            "sign": self.sign,
            "timestamp": timestamp
        }).json()
        return res['data']['repData']

    def solve_captcha(self, mode='clickWord'):
        def h(*wargs):
            """
            使用 AES 加密算法对输入进行加密，并返回 Base64 编码的结果。
            内部函数 pkcs7 用于填充数据，使其符合 AES 加密的要求。
            """
            def pkcs7(m):
                return m + (chr(16 - len(m) % 16) * (16 - len(m) % 16)).encode('utf-8')

            t = wargs[1] if len(wargs) > 1 and wargs[1] != 0 else 'XwKsGlMcdPMEhR1B'
            s = t.encode('utf-8')
            i = wargs[0].encode('utf-8')
            cipher = AES.new(s, AES.MODE_ECB)
            text = base64.b64encode(cipher.encrypt(pkcs7(i))).decode('utf-8')
            return text

        def to_str(x):
            return json.dumps(x, separators=(',', ':'))

        if mode == 'clickWord':
            while True:
                data = self.solve_click_word()

                # with open("click_word.json", "w", encoding="utf-8") as f:
                #     json.dump(data, f, ensure_ascii=False, indent=4)

                base64_img = data['originalImageBase64']
                word_list = data['wordList']
                token = data['token']
                point_arr = self.ocr_captcha(base64_img, word_list) # ocr，输入base64编码的图像，按顺序返回字的坐标。决定是否能过验证的关键一步。

                # 如果数据中包含 secretKey，使用它对识别结果 point_arr 进行加密，否则直接加密。
                if 'secretKey' in data.keys():
                    secret_key = data['secretKey']
                    point_json = h(to_str(point_arr), secret_key)
                else:
                    point_json = h(to_str(point_arr))

                params = {
                    'captchaType': mode,
                    'pointJson': point_json,
                    'token': token
                }
                timestamp = self.get_timestamp()
                self.sign = self.get_sign(timestamp=timestamp, params=params, path="/api/captcha/check")
                res = self.sess.post(self.check_captcha_url, headers={
                    "accept": "application/json, text/plain, */*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "app-key": "8fceb735082b5a529312040b58ea780b",
                    "cgauthorization": self.access_token,
                    "content-type": "application/x-www-form-urlencoded; charset=utf-8",
                    "sign": self.sign,
                    "timestamp": timestamp
                }, params=params)
                if res.json()['data']['repCode'] == '0000':
                    return h(token + '---' + to_str(point_arr), secret_key) if 'secretKey' in data.keys() else h(
                        token + '---' + to_str(point_arr))
                else:
                    logger.info(f'验证码错误，重试中...')
                    time.sleep(0.1) # 有时貌似验证图片没有刷新出来，是不是这里的延时太短了？
        elif mode == 'blockPuzzle':
            # 暂时没用到
            raise NotImplementedError


class LoginError(Exception):
    """Login Exception"""
    pass


def listener(event):
    jobs = schedule.get_jobs()
    if event.retval is None:
        if len(jobs) > 0:
            schedule.remove_job(schedule.get_jobs()[0].id)
    elif event.retval['code'] in [200, 409]:
        if len(jobs) > 0:
            schedule.remove_job(schedule.get_jobs()[0].id)

    if len(jobs) == 0:
        schedule.shutdown(wait=False)


def job(user, buddies, reserver, mode):
    if int(args.buddy) < 0:
        buddy_no = ""
        for buddy in buddies:
            tmp = User(buddy['username'], buddy['password'])
            if buddy_no != "":
                buddy_no += ","
            buddy_no += str(tmp.get_buddy_no())
        logger.info(f'buddy_no: {buddy_no}')
    else:
        buddy_no = int(args.buddy)
    return user.exec(buddy_no, reserver, mode)


def main():
    config = json.load(open("./config.json", encoding="utf-8"))
    username = config['username']
    password = config['password']

    resever = Reserver()

    main_user = User(username, password)

    logger.info(args)

    # 这行代码中的add_listener函数用于给调度器加上一个事件监听器。具体来说，程序希望监听两种事件：
    # EVENT_JOB_EXECUTED：当一个任务执行完毕且没有异常时，触发这个事件。
    # EVENT_JOB_ERROR：当一个任务执行时出现错误，触发这个事件。
    # 当这些事件发生时，listener函数就会被调用。通常，listener函数会定义一些处理逻辑，比如在任务完成后记录日志或者在任务出错时发送警报。
    schedule.add_listener(listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    run_time = datetime.datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=tz.gettz('Asia/Shanghai'))
    
    if args.mode == 'interval':
        schedule.add_job(job, 'interval', seconds=10,
                         args=[main_user, config['buddies'], resever, args.mode],
                         start_date=run_time)
        schedule.print_jobs()
        schedule.start()
    elif args.mode == 'once':
        schedule.add_job(job, 'date', next_run_time=run_time, args=[main_user, config['buddies'], resever, args.mode])
        schedule.print_jobs()
        schedule.start()
    elif args.mode == 'debug': # 不采用schedule，直接执行job。可忽略上面所有和schedule相关的代码
        job(main_user, config['buddies'], resever, args.mode)


if __name__ == "__main__":
    sys.stdout = open(os.devnull, 'w') # 程序中所有试图打印到标准输出（通常是控制台）的内容都会被丢弃，不会显示在控制台上。
    main()
