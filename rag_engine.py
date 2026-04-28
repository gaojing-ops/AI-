# -*- coding: utf-8 -*-
"""
轻量级本地化 RAG 引擎 (TF-IDF + Cosine Similarity)
无大型数据库依赖，适用于小说这种中短篇幅的世界观和人物小传匹配。
"""
import os
import math
import collections
import jieba

class SimpleLocalRAG:
    def __init__(self):
        self.documents = {} # {id: {"path": path, "title": title, "content": text, "tokens": [...]}}
        self.doc_freqs = collections.defaultdict(int)
        self.total_docs = 0

    def get_tokens(self, text):
        """精准分词，过滤标点"""
        words = jieba.lcut(text)
        return [w for w in words if len(w.strip()) > 0 and w not in ['，', '。', '！', '？', '、', '：', '；', '“', '”', '‘', '’', '\n', '\r', ' ']]

    def add_document(self, doc_id, filepath, title, content):
        if not content.strip():
            return
            
        tokens = self.get_tokens(title + "\n" + content)
        if not tokens:
            return
            
        # 统计词频 (TF)
        tf = collections.Counter(tokens)
        
        # 统计文档频次 (DF)
        unique_tokens = set(tokens)
        for token in unique_tokens:
            self.doc_freqs[token] += 1
            
        # 预计算文档向量范数，避免每次 search 时重复计算
        doc_len = len(tokens)
        doc_norm = 0
        for token, count in tf.items():
            df = self.doc_freqs.get(token, 1)
            idf_val = math.log((self.total_docs + 1) / (df + 1)) + 1
            w = (count / doc_len) * idf_val
            doc_norm += w ** 2
        doc_norm = math.sqrt(doc_norm) if doc_norm > 0 else 0

        self.documents[doc_id] = {
            "path": filepath,
            "title": title,
            "content": content,
            "tf": tf,
            "length": doc_len,
            "norm": doc_norm,
        }
        self.total_docs = len(self.documents)

    def search(self, query, top_k=3, threshold=0.01):
        """基于查询文本搜索最相关的文档"""
        if not self.documents:
            return []
            
        query_tokens = self.get_tokens(query)
        if not query_tokens:
            return []
            
        query_tf = collections.Counter(query_tokens)
        
        # 计算IDF缓存
        idf = {}
        for token in query_tf:
            df = self.doc_freqs.get(token, 0)
            if df > 0:
                idf[token] = math.log((self.total_docs + 1) / (df + 1)) + 1
            else:
                # OOV (Out Of Vocabulary) 词语给一个默认平滑IDF
                idf[token] = math.log((self.total_docs + 1) / 1) + 1
                
        # 计算 Query 向量的长度 (分母)
        query_norm = 0
        query_vec = {}
        for token, count in query_tf.items():
            weight = (count / len(query_tokens)) * idf[token]
            query_vec[token] = weight
            query_norm += weight ** 2
        query_norm = math.sqrt(query_norm)

        scores = []
        for doc_id, doc in self.documents.items():
            doc_tf = doc["tf"]
            doc_len = doc["length"]
            doc_norm = doc.get("norm", 0)

            # 计算 Doc 向量和点积 (分子) — 仅计算 query 中存在的词
            dot_product = 0
            for token, q_weight in query_vec.items():
                if token in doc_tf:
                    doc_weight = (doc_tf[token] / doc_len) * idf[token]
                    dot_product += q_weight * doc_weight
            
            if query_norm > 0 and doc_norm > 0:
                similarity = dot_product / (query_norm * doc_norm)
                if similarity > threshold:
                    scores.append((doc_id, similarity, doc['title'], doc['content']))
                    
        # 排序并返回前 K 个
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

# 测试用例
if __name__ == "__main__":
    rag = SimpleLocalRAG()
    rag.add_document("doc1", "", "李青", "李青，男，25岁，剑宗真传弟子，擅长使用青莲剑歌，为人冷酷无情。")
    rag.add_document("doc2", "", "苏灵儿", "苏灵儿，魔宗圣女，修炼天魔魅影，性格古灵精怪，一直暗中观察李青。")
    rag.add_document("doc3", "", "青莲剑宗", "修仙界第一大宗门，门规森严，剑法天下无双。")
    
    query = "李青独自走在小路上，突然感受到一股魔气，他拔出剑准备战斗。"
    results = rag.search(query, top_k=2)
    print(f"Query: {query}")
    for res in results:
        print(f"匹配: {res[2]} (Score: {res[1]:.4f})")
