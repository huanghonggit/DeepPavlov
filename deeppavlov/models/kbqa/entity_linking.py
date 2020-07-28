# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import sqlite3
import time
from logging import getLogger
from typing import List, Dict, Tuple, Optional, Any
from collections import defaultdict, Counter

import numpy as np
import nltk
import pymorphy2
import faiss
from nltk.corpus import stopwords
from nltk import sent_tokenize
from rapidfuzz import fuzz
from hdt import HDTDocument
from sklearn.feature_extraction.text import TfidfVectorizer

from deeppavlov.core.common.registry import register
from deeppavlov.core.models.component import Component
from deeppavlov.core.common.chainer import Chainer
from deeppavlov.core.models.serializable import Serializable
from deeppavlov.core.commands.utils import expand_path
from deeppavlov.core.common.file import load_pickle, save_pickle
from deeppavlov.models.kbqa.entity_detection_parser import EntityDetectionParser
from deeppavlov.models.kbqa.rel_ranking_bert_infer import RelRankerBertInfer

log = getLogger(__name__)


@register('ner_chunker')
class NerChunker(Component):
    def __init__(self, max_chunk_len: int = 300, batch_size: int = 5, **kwargs):
        self.max_chunk_len = max_chunk_len
        self.batch_size = batch_size

    def __call__(self, docs_batch: List[str]) -> Tuple[List[List[str]], List[List[int]]]:
        text_batch_list = []
        text_batch = []
        nums_batch_list = []
        nums_batch = []
        count_texts = 0
        text = ""
        for n, doc in enumerate(docs_batch):
            sentences = sent_tokenize(doc)
            for sentence in sentences:
                if len(text) + len(sentence) < self.max_chunk_len:
                    text += sentence
                    text += " "
                else:
                    if count_texts < self.batch_size:
                        text_batch.append(text.strip(" "))
                        nums_batch.append(n)
                        count_texts  += 1
                    else:
                        text_batch_list.append(text_batch)
                        text_batch = []
                        nums_batch_list.append(nums_batch)
                        nums_batch = [n]
                        count_texts = 0
                    text = sentence + " "
                    
        if text:
            text_batch.append(text.strip(" "))
            text_batch_list.append(text_batch)
            nums_batch.append(len(docs_batch)-1)
            nums_batch_list.append(nums_batch)
                    
        return text_batch_list, nums_batch_list


@register('entity_linker')
class EntityLinker(Component, Serializable):
    def __init__(self, load_path: str,
                 word_to_idlist_filename: str,
                 entities_list_filename: str,
                 entities_ranking_filename: str,
                 vectorizer_filename: str,
                 faiss_index_filename: str,
                 chunker: NerChunker = None,
                 ner: Chainer = None,
                 ner_parser: EntityDetectionParser = None,
                 entity_ranker: RelRankerBertInfer = None,
                 num_faiss_candidate_entities: int = 20,
                 num_entities_for_bert_ranking: int = 50,
                 num_faiss_cells: int = 50,
                 use_gpu: bool = True,
                 save_path: str = None,
                 fit_vectorizer: bool = False,
                 max_tfidf_features: int = 1000,
                 include_mention: bool = False,
                 ngram_range: List[int] = None,
                 num_entities_to_return: int = 10,
                 build_inverted_index: bool = False,
                 kb_format: str = "hdt",
                 kb_filename: str = None,
                 label_rel: str = None,
                 descr_rel: str = None,
                 aliases_rels: List[str] = None,
                 sql_table_name: str = None,
                 sql_column_names: List[str] = None,
                 lang: str = "ru",
                 use_descriptions: bool = True,
                 lemmatize: bool = False,
                 use_prefix_tree: bool = False,
                 **kwargs) -> None:
        super().__init__(save_path=save_path, load_path=load_path)
        self.morph = pymorphy2.MorphAnalyzer()
        self.lemmatize = lemmatize
        self.word_to_idlist_filename = word_to_idlist_filename
        self.entities_list_filename = entities_list_filename
        self.entities_ranking_filename = entities_ranking_filename
        self.vectorizer_filename = vectorizer_filename
        self.faiss_index_filename = faiss_index_filename
        self.num_entities_for_bert_ranking = num_entities_for_bert_ranking
        self.num_faiss_candidate_entities = num_faiss_candidate_entities
        self.num_faiss_cells = num_faiss_cells
        self.use_gpu = use_gpu
        self.chunker = chunker
        self.ner = ner
        self.ner_parser = ner_parser
        self.entity_ranker = entity_ranker
        self.fit_vectorizer = fit_vectorizer
        self.max_tfidf_features = max_tfidf_features
        self.include_mention = include_mention
        self.ngram_range = ngram_range
        self.num_entities_to_return = num_entities_to_return
        self.build_inverted_index = build_inverted_index
        self.kb_format = kb_format
        self.kb_filename = kb_filename
        self.label_rel = label_rel
        self.aliases_rels = aliases_rels
        self.descr_rel = descr_rel
        self.sql_table_name = sql_table_name
        self.sql_column_names = sql_column_names
        self.inverted_index: Optional[Dict[str, List[Tuple[str]]]] = None
        self.lang_str = f"@{lang}"
        if self.lang_str == "@en":
            self.stopwords = set(stopwords.words("english"))
        elif self.lang_str == "@ru":
            self.stopwords = set(stopwords.words("russian"))
        self.re_tokenizer = re.compile(r"[\w']+|[^\w ]")
        self.use_descriptions = use_descriptions

        if self.build_inverted_index:
            if self.kb_format == "hdt":
                self.doc = HDTDocument(str(expand_path(self.kb_filename)))
            if self.kb_format == "sqlite3":
                self.conn = sqlite3.connect(str(expand_path(self.kb_filename)))
                self.cursor = self.conn.cursor()
            self.inverted_index_builder()
            self.save()
        else:
            self.load()

        if self.fit_vectorizer:
            self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=tuple(self.ngram_range),
                                              max_features=self.max_tfidf_features, max_df=0.85)
            self.vectorizer.fit(self.word_list)
            self.matrix = self.vectorizer.transform(self.word_list)
            self.dense_matrix = self.matrix.toarray()
            if self.num_faiss_cells > 1:
                quantizer = faiss.IndexFlatIP(self.max_tfidf_features)
                self.faiss_index = faiss.IndexIVFFlat(quantizer, self.max_tfidf_features, self.num_faiss_cells)
                self.faiss_index.train(self.dense_matrix.astype(np.float32))
            else:
                self.faiss_index = faiss.IndexFlatIP(self.max_tfidf_features)
                if self.use_gpu:
                    res = faiss.StandardGpuResources()
                    self.faiss_index = faiss.index_cpu_to_gpu(res, 0, self.faiss_index)
            self.faiss_index.add(self.dense_matrix.astype(np.float32))
            self.save_vectorizers_data()

    def load(self) -> None:
        self.word_to_idlist = load_pickle(self.load_path / self.word_to_idlist_filename)
        self.entities_list = load_pickle(self.load_path / self.entities_list_filename)
        self.word_list = list(self.word_to_idlist.keys())
        self.entities_ranking_dict = load_pickle(self.load_path / self.entities_ranking_filename)
        if not self.fit_vectorizer:
            self.vectorizer = load_pickle(self.load_path / self.vectorizer_filename)
            self.faiss_index = faiss.read_index(str(expand_path(self.faiss_index_filename)))
            if self.use_gpu:
                res = faiss.StandardGpuResources()
                self.faiss_index = faiss.index_cpu_to_gpu(res, 0, self.faiss_index)

    def save(self) -> None:
        save_pickle(self.inverted_index, self.save_path / self.inverted_index_filename)
        save_pickle(self.entities_list, self.save_path / self.entities_list_filename)
        save_pickle(self.q2name, self.save_path / self.q2name_filename)
        if self.q2descr_filename is not None:
            save_pickle(self.q2descr, self.save_path / self.q2descr_filename)

    def save_vectorizers_data(self) -> None:
        save_pickle(self.vectorizer, self.save_path / self.vectorizer_filename)
        faiss.write_index(self.faiss_index, str(expand_path(self.faiss_index_filename)))

    def __call__(self, docs_batch: List[List[str]]):
        text_batch_list, nums_batch_list = self.chunker(docs_batch)
        entity_ids_batch_list = []
        for text_batch in text_batch_list:
            entity_ids_batch = []
            ner_tokens_batch, ner_probas_batch = self.ner(text_batch)
            entity_substr_batch, _, entity_positions_batch = self.ner_parser(ner_tokens_batch, ner_probas_batch)
            log.debug(f"entity_substr_batch {entity_substr_batch}")
            log.debug(f"entity_positions_batch {entity_positions_batch}")
            entity_substr_batch = [[entity_substr.lower() for tag, entity_substr_list in entity_substr_dict.items()
                                                         for entity_substr in entity_substr_list]
                                                         for entity_substr_dict in entity_substr_batch]
            entity_positions_batch = [[entity_positions for tag, entity_positions_list in entity_positions_dict.items()
                                                         for entity_positions in entity_positions_list]
                                                         for entity_positions_dict in entity_positions_batch]
            log.debug(f"entity_substr_batch {entity_substr_batch}")
            log.debug(f"entity_positions_batch {entity_positions_batch}")
            for entity_substr_list, entity_positions_list, context_tokens in zip(entity_substr_batch, entity_positions_batch, ner_tokens_batch):
                if entity_substr_list:
                    entity_ids_list = self.link_entities(entity_substr_list, entity_positions_list, context_tokens)
                    entity_ids_batch.append(entity_ids_list)
            entity_ids_batch_list.append(entity_ids_batch)
        return entity_ids_batch_list

    def link_entities(self, entity_substr_list: List[str], entity_positions_list: List[List[int]] = None,
                          context_tokens: List[str] = None) -> List[List[str]]:
        log.debug(f"context_tokens {context_tokens}")
        log.debug(f"entity substr list {entity_substr_list}")
        log.debug(f"entity positions list {entity_positions_list}")
        entity_ids_list = []
        if entity_substr_list:
            entity_substr_list = [[word for word in entity_substr.split(' ') if word not in self.stopwords and len(word) > 0]
                                        for entity_substr in entity_substr_list]
            words_and_indices  = [(self.morph_parse(word), i) for i, entity_substr in enumerate(entity_substr_list) for word in entity_substr]
            substr_lens = [len(entity_substr) for entity_substr in entity_substr_list]
            log.debug(f"words and indices {words_and_indices}")
            words, indices = zip(*words_and_indices)
            words = list(words)
            indices = list(indices)
            log.debug(f"words {words}")
            log.debug(f"indices {indices}")
            ent_substr_tfidfs = self.vectorizer.transform(words).toarray().astype(np.float32)
            D, I = self.faiss_index.search(ent_substr_tfidfs, self.num_faiss_candidate_entities)
            candidate_entities_dict = defaultdict(list)
            for ind_list, scores_list, index in zip(I, D, indices):
                candidate_entities = {}
                for ind, score in zip(ind_list, scores_list):
                    start_ind, end_ind = self.word_to_idlist[self.word_list[ind]]
                    for entity in self.entities_list[start_ind:end_ind]:
                        if entity in candidate_entities:
                            if score > candidate_entities[entity]:
                                candidate_entities[entity] = score
                        else:
                            candidate_entities[entity] = score
                candidate_entities_dict[index] += [(entity, cand_entity_len, score) for (entity, cand_entity_len), score in candidate_entities.items()]
                log.debug(f"{index} candidate_entities {[self.word_list[ind] for ind in ind_list[:10]]}")
            candidate_entities_total = list(candidate_entities_dict.values())
            candidate_entities_total = [self.sum_scores(candidate_entities, substr_len)
                              for candidate_entities, substr_len in zip(candidate_entities_total, substr_lens)]
            log.debug(f"length candidate entities list {len(candidate_entities_total)}")
            candidate_entities_list = []
            entities_scores_list = []
            for candidate_entities in candidate_entities_total:
                log.debug(f"candidate_entities before ranking {candidate_entities[:10]}")
                candidate_entities = [candidate_entity + (self.entities_ranking_dict.get(candidate_entity[0], 0),)
                                               for candidate_entity in candidate_entities]
                candidate_entities_str = '\n'.join([str(candidate_entity) for candidate_entity in candidate_entities])
                candidate_entities = sorted(candidate_entities, key=lambda x: (x[1], x[2]), reverse=True)
                log.debug(f"candidate_entities {candidate_entities[:10]}")
                entities_scores = {entity: (substr_score, pop_score) for entity, substr_score, pop_score in candidate_entities}
                candidate_entities = [candidate_entity[0] for candidate_entity in candidate_entities][:self.num_entities_for_bert_ranking]
                log.debug(f"candidate_entities {candidate_entities[:10]}")
                candidate_entities_list.append(candidate_entities)
                entity_ids_list.append(candidate_entities[:self.num_entities_to_return])
                entities_scores_list.append(entities_scores)
            if self.use_descriptions:
                entity_ids_list = self.rank_by_description(entity_positions_list, candidate_entities_list, entities_scores_list, context_tokens)

        return entity_ids_list

    def morph_parse(self, word):
        morph_parse_tok = self.morph.parse(word)[0]
        normal_form = morph_parse_tok.normal_form
        return normal_form

    def sum_scores(self, candidate_entities: List[Tuple[str, int]], substr_len: int) -> List[Tuple[str, float]]:
        entities_with_scores_sum = defaultdict(int)
        for entity in candidate_entities:
            entities_with_scores_sum[(entity[0], entity[1])] += entity[2]
        
        entities_with_scores = {}
        for (entity, cand_entity_len), scores_sum in entities_with_scores_sum.items():
            score = min(scores_sum, cand_entity_len)/max(substr_len, cand_entity_len)
            if entity in entities_with_scores:
                if score > entities_with_scores[entity]:
                    entities_with_scores[entity] = score
            else:
                entities_with_scores[entity] = score
        entities_with_scores = list(entities_with_scores.items())
        
        return entities_with_scores

    def rank_by_description(self, entity_positions_list: List[List[int]],
                                  candidate_entities_list: List[List[str]],
                                  entities_scores_list: List[Dict[str, Tuple[int, float]]],
                                  context_tokens: List[str]) -> List[List[str]]:
        entity_ids_list = []
        for entity_pos, candidate_entities, entities_scores in zip(entity_positions_list, candidate_entities_list, entities_scores_list):
            log.debug(f"entity_pos {entity_pos}")
            log.debug(f"candidate_entities {candidate_entities[:10]}")
            if self.include_mention:
                context = ' '.join(context_tokens[:entity_pos[0]]+["[ENT]"]+
                                                   context_tokens[entity_pos[0]:entity_pos[-1]+1]+["[ENT]"]+
                                                   context_tokens[entity_pos[-1]+1:])
            else:
                context = ' '.join(context_tokens[:entity_pos[0]]+["[ENT]"] + context_tokens[entity_pos[-1]+1:])
            log.debug(f"context {context}")
            log.debug(f"len candidate entities {len(candidate_entities)}")
            scores = self.entity_ranker.rank_rels(context, candidate_entities)
            entities_with_scores = [(entity, round(entities_scores[entity][0], 2), entities_scores[entity][1], round(score,2))
                                                                                                 for entity, score in scores]
            log.debug(f"len entities with scores {len(entities_with_scores)}")
            entities_with_scores = [entity for entity in entities_with_scores if entity[3] > 0.1]
            entities_with_scores = sorted(entities_with_scores, key=lambda x: (x[1], x[3], x[2]), reverse=True)
            log.debug(f"entities_with_scores {entities_with_scores}")
            top_entities = [score[0] for score in entities_with_scores]
            entity_ids_list.append(top_entities[:10])
        return entity_ids_list
