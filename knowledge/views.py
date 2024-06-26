import base64
import os
import secrets
import shutil

from api_services.google_storage_api import GoogleStorageAPI
from constant_variables.configs import (
    BUCKET_NAME,
    CONTEXTUALIZE_Q_SYSTEM_PROMPT,
    DATABASE_URL,
    DEFAULT_SYSTEM_PROMPT,
    DOC_EXTENSIONS,
    LANGSMITH_KEY,
    OPENAI_BASE_URL,
    OPENAI_CHAT_MODEL,
    OPENAI_EMBEDDINGS_MODEL,
)
from django.core.files.storage import FileSystemStorage
from django.http import JsonResponse
from helpers.db_helper import DBHelper
from helpers.embeddings_helper import EmbeddingsHelper
from helpers.file_helper import FileHelper
from helpers.google_api_helper import GoogleApiHelper
from knowledge.models import Knowledge
from knowledge.serializers import (
    KnowledgeCreateSerializer,
    KnowledgeDeleteSerializer,
    KnowledgeRetrieveSerializer,
)
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain_community.vectorstores.pgvector import PGVector
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_openai import ChatOpenAI
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from rest_framework.views import APIView
from vector_db.models import Collection

os.environ["LANGCHAIN_PROJECT"] = "retrieval_api"
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"] = LANGSMITH_KEY


class KnowledgeView(APIView):
    def __init__(self):
        self.google_storage = GoogleStorageAPI()
        self.file_helper = FileHelper()
        self.google_api_helper = GoogleApiHelper()
        self.connection_string = DATABASE_URL

    def post(self, request):
        try:
            serializer = KnowledgeCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            validated_data = serializer.validated_data
            file = validated_data.get("file")

            file_name = file.name
            _, file_extension = os.path.splitext(file_name)
            file_extension = file_extension.lower()

            if file_extension not in DOC_EXTENSIONS:
                return JsonResponse({"message": "Please upload a txt file"}, status=400)

            safe_string = base64.urlsafe_b64encode(secrets.token_bytes(24)).decode(
                "utf-8"
            )
            tmp_storage_path = f"tmp/{safe_string}"

            os.makedirs(tmp_storage_path, exist_ok=True)
            file_path = os.path.join(tmp_storage_path, file_name)
            FileSystemStorage(location=tmp_storage_path).save(file_name, file)

            storage_path = f"knowledges/{safe_string}_{file_name}"
            knowledge_url = self.google_storage.upload_blob(
                BUCKET_NAME, file_path, storage_path
            )
            openai_api_key = validated_data.get("openai_api_key")
            os.environ["OPENAI_API_KEY"] = openai_api_key

            loader = self.file_helper.get_loader(file_path, file_extension)

            if loader is None:
                return JsonResponse(
                    {"message": f"File extension {file_extension} is not supported"},
                    status=400,
                )

            knowledge = loader.load()
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000,
                chunk_overlap=200,
                length_function=len,
            )
            docs = text_splitter.split_documents(knowledge)
            embeddings = OpenAIEmbeddings(
                check_embedding_ctx_length=False,
                base_url=OPENAI_BASE_URL,
                model=OPENAI_EMBEDDINGS_MODEL,
            )
            collection_name = f"langchain_{safe_string}"

            PGVector.from_documents(
                embedding=embeddings,
                documents=docs,
                collection_name=collection_name,
                connection_string=self.connection_string,
                use_jsonb=True,
            )

            collection_id = Collection.objects.get(name=collection_name).uuid

            Knowledge.objects.create(
                url=knowledge_url,
                collection_id=collection_id,
            )
            shutil.rmtree(tmp_storage_path)

            return JsonResponse({"message": "Successfully created embeddings"})
        except Exception as e:
            shutil.rmtree(tmp_storage_path)
            error_message = str(e)
            return JsonResponse({"message": error_message}, status=500)

    def delete(self, request):
        try:
            serializer = KnowledgeDeleteSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            validated_data = serializer.validated_data

            url = validated_data.get("url")

            openai_api_key = validated_data.get("openai_api_key")
            os.environ["OPENAI_API_KEY"] = openai_api_key

            collection_id = Knowledge.objects.get(url=url).collection_id
            collection_name = Collection.objects.get(uuid=collection_id).name

            embeddings = OpenAIEmbeddings()

            store = PGVector(
                connection_string=self.connection_string,
                collection_name=collection_name,
                embedding_function=embeddings,
                use_jsonb=True,
            )
            store.delete_collection()

            Knowledge.objects.filter(url=url).delete()

            blob_name = self.google_api_helper.get_blob_name(url)
            self.google_storage.delete_blob(BUCKET_NAME, blob_name)

            return JsonResponse({"message": "Successfully deleted"})
        except Exception as e:
            error_message = str(e)
            return JsonResponse({"message": error_message}, status=500)


class KnowledgeRetrieveView(APIView):
    def __init__(self):
        self.db_helper = DBHelper()
        self.embeddings_helper = EmbeddingsHelper()
        self.connection_string = DATABASE_URL

    def post(self, request):
        try:
            serializer = KnowledgeRetrieveSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            validated_data = serializer.validated_data

            system_prompt = DEFAULT_SYSTEM_PROMPT
            question = validated_data.get("question")
            openai_api_key = validated_data.get("openai_api_key")
            user_id = validated_data.get("user_id")

            os.environ["OPENAI_API_KEY"] = openai_api_key

            if self.db_helper.have_embeddings() is False:
                return JsonResponse(
                    {"message": "No embeddings found. Please create embeddings first"},
                    status=200,
                )

            embeddings = self.embeddings_helper.generate_embeddings(
                openai_api_key, question
            )
            top3_collection_ids = self.embeddings_helper.get_top3_similar_collections(
                embeddings
            )
            collection_id = self.embeddings_helper.get_most_similar_collection(
                top3_collection_ids
            )
            collection_name = Collection.objects.get(uuid=collection_id).name
            session_id = user_id
            llm = ChatOpenAI(
                model=OPENAI_CHAT_MODEL,
                base_url=OPENAI_BASE_URL,
            )
            embeddings = OpenAIEmbeddings(
                check_embedding_ctx_length=False,
                base_url=OPENAI_BASE_URL,
                model=OPENAI_EMBEDDINGS_MODEL,
            )
            connection_string = self.connection_string

            store = PGVector(
                connection_string=connection_string,
                embedding_function=embeddings,
                collection_name=collection_name,
                use_jsonb=True,
            )
            retriever = store.as_retriever()

            ### Contextualize question ###
            contextualize_q_system_prompt = CONTEXTUALIZE_Q_SYSTEM_PROMPT
            contextualize_q_prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", contextualize_q_system_prompt),
                    MessagesPlaceholder("chat_history"),
                    ("human", "{input}"),
                ]
            )
            history_aware_retriever = create_history_aware_retriever(
                llm, retriever, contextualize_q_prompt
            )

            ### Answer question ###
            qa_system_prompt = system_prompt + """{context}"""
            qa_prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", qa_system_prompt),
                    MessagesPlaceholder("chat_history"),
                    ("human", "{input}"),
                ]
            )
            question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)
            rag_chain = create_retrieval_chain(
                history_aware_retriever, question_answer_chain
            )

            ### Statefully manage chat history ###
            conversational_rag_chain = RunnableWithMessageHistory(
                rag_chain,
                lambda session_id: RedisChatMessageHistory(
                    session_id, url="redis://localhost:6379"
                ),
                input_messages_key="input",
                history_messages_key="chat_history",
                output_messages_key="answer",
            )
            answer = conversational_rag_chain.invoke(
                {"input": question},
                config={"configurable": {"session_id": session_id}},
            )["answer"]

            return JsonResponse({"message": answer})
        except Exception as e:
            error_message = str(e)
            return JsonResponse({"message": error_message}, status=500)
