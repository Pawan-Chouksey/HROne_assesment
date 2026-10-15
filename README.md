# Employee Attendance & Analytics API

## Local setup

1. **Start MongoDB:**
   - **Local instance (Docker):**
     ```bash
     docker run -d -p 27017:27017 --name mongo mongo:7
     ```
   - **Or MongoDB Atlas (Free Tier M0):**
     Create a free cluster on MongoDB Atlas and copy your connection string.

2. **Configure environment variables (Optional):**
   If using Atlas or custom credentials, create a `.env` file from the example:
   ```bash
   cp .env.example .env
   ```
   Example `.env` for MongoDB Atlas:
   ```text
   MONGO_URI=mongodb+srv://<username>:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority
   MONGO_DB=attendance_db
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Load sample data into MongoDB:**
   ```bash
   python sample_seed.py
   ```

5. **Start the API server:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

Interactive API documentation will be available at `http://localhost:8000/docs`.

## Notes

- All application code is in `app/main.py`.
- Indexes are created automatically at startup.
- Review findings and architectural decisions are documented in `REVIEW.md` and `DECISIONS.md`.
