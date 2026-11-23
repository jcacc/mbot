import logging, sys
from cyberlink import CyberPi
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
bot = CyberPi()
print("port:", bot.port)
try:
    bot.connect()
    print("connected")
    for expr in sys.argv[1:]:
        try:
            print(f"{expr!r:55} -> {bot.call(expr)!r}")
        except Exception as e:
            print(f"{expr!r:55} !! {e}")
finally:
    print("last raw frames:")
    for f in bot.raw_log[-4:]:
        print("  ", f.hex(" "))
    bot.close()
