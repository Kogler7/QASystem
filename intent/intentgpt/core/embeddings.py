from sklearn.neighbors import NearestNeighbors
import pickle
import os
import hashlib
import pandas as pd
from ..utils.embedding_utils import get_embedder
import sqlite3

# Function to generate hash key from string
def generate_key(input_string):
    return hashlib.sha256(input_string.encode('utf-8')).hexdigest()

class Embedder():
    def __init__(self, embedder_name):
        self.embedder_name = embedder_name
        self.init_embedder()

    def init_embedder(self):
        self.embedder = get_embedder(self.embedder_name)
    
    def embed_texts(self, text_list):
        # Use SentenceTransformer encode method
        embeddings = self.embedder.encode(text_list)
        return embeddings
        

class EmbeddingDB():
    def __init__(self, embedder, texts = None, db_file='embedding_db.sqlite'):
        self.embedder = embedder
        self.db_file = db_file
        self.init_directories()
        self.init_db()
        self.stored_embeddings = {}

        if texts is not None:
            for text in texts:
                self.get_embedding(text)
                
        
    def add_embedding(self, input_string, embedding):
        key = generate_key(input_string)
        pickle_path = os.path.join(self.embeddings_dir, f"{key}.pickle")
        with open(pickle_path, 'wb') as f:
            pickle.dump(embedding, f)

        conn, cursor = self.db_connect()
        cursor.execute("INSERT INTO embeddings (key, embedding_path) VALUES (?, ?)", (key, pickle_path))
        conn.commit()
        conn.close()

    def get_embedding(self, input_string):
        if input_string in self.stored_embeddings:
            embedding = self.stored_embeddings[input_string]
        else:
            key = generate_key(input_string)  # You need to define generate_key()
            conn, cursor = self.db_connect()
            cursor.execute("SELECT embedding_path FROM embeddings WHERE key=?", (key,))
            row = cursor.fetchone()
            conn.close()
            if row is not None:
                embedding_path = row[0]
                with open(embedding_path, 'rb') as f:
                    embedding = pickle.load(f)
            else:
                embedding = self.embedder.embed_texts(input_string)
                self.add_embedding(input_string, embedding)

        self.stored_embeddings[input_string] = embedding
        return embedding
    
    def get_k_nearest(self, batch_embeddings, k):
        stored_embeddings_list = list(self.stored_embeddings.values())        
        neighbors = NearestNeighbors(n_neighbors=k,
                         metric='cosine',
                         algorithm='brute',
                         n_jobs=8).fit(stored_embeddings_list)
        
        distances, indices = neighbors.kneighbors(batch_embeddings)
        return distances, indices

    def db_connect(self):
        conn = sqlite3.connect(self.db_file_path)
        cursor = conn.cursor()
        return conn, cursor
    
    def init_db(self):
        conn, cursor = self.db_connect()
        cursor.execute('''CREATE TABLE IF NOT EXISTS embeddings
                          (key TEXT PRIMARY KEY, embedding_path TEXT)''')
        conn.commit()
        conn.close()

    def select_all(self):
        conn, cursor = self.db_connect()
        cursor.execute("SELECT * FROM embeddings")
        rows = cursor.fetchall()
        conn.close()
        return rows
    
    def init_directories(self):
        root_dir = os.getcwd()
        self.db_file_path = os.path.join(root_dir, 'embeddings', self.db_file)
        self.embeddings_dir = os.path.join(root_dir, 'embeddings', self.db_file.split(".")[0], self.embedder.embedder_name)
        os.makedirs(self.embeddings_dir, exist_ok=True)
if __name__ == "__main__":
    texts = ['hello', 'world', 'how', 'are', 'you']
    embedder = Embedder('sentence-transformer')
    db = EmbeddingDB(embedder, texts, 'test.sqlite')



