import requests

url = 'https://nim-nosdn.netease.im/MTExNTUxNzE=/bmltYV8zMTYzMjYyMjA3MzlfMTc1NzkyMDA3MzY0Nl85MTdlZjNkMi01NDFiLTRiNTItYjdjYi1jMjQzMjQ4ZTMyODg=?createTime=1757920097&survivalTime=31536000&twmid=8003318855354242'

# 显式禁用代理
img_data = requests.get(url, proxies={"http": None, "https": None}, timeout=10).content

# 保存图片（可选）
with open("downloaded_image.jpg", "wb") as f:
    f.write(img_data)