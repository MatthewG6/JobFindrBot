from fastapi import FastAPI

app = FastAPI(title="Job Radar Assistant")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Job Radar Assistant is running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
