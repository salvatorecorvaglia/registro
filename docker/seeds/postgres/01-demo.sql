CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    amount NUMERIC(10, 2) NOT NULL,
    placed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO users (email) VALUES
    ('ada@example.com'),
    ('grace@example.com'),
    ('alan@example.com')
ON CONFLICT DO NOTHING;
