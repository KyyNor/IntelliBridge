from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tools.hive_query import router as hive_router
from tools.agent_browser import router as agent_browser_router

app = FastAPI(
    title="IntelliBridge API",
    description="IntelliBridge Backend Service",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Welcome to IntelliBridge API"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# 注册路由
app.include_router(hive_router)
app.include_router(agent_browser_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=49000)
