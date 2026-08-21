import sys, time
sys.path.insert(0, r"C:\Users\user\Desktop\PSA-Backend-code\pc-meetings\backend")
from dotenv import load_dotenv
load_dotenv(r"C:\Users\user\Desktop\PSA-Backend-code\pc-meetings\.env", override=True)
from app.routers import programs as p

def timeit(label, fn):
    t0 = time.time()
    r = fn()
    print(f"{label}: {time.time()-t0:.2f}s")
    return r

rs = timeit("role_summary", lambda: p.role_summary(year=2026, month=8))
for r in rs: print(" ", r)
