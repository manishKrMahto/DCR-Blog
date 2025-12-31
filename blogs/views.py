from django.shortcuts import render, get_object_or_404
from django.http import HttpResponseRedirect
from django.db.models import Q

from .models import Blog, Category, Comment

import random
from dotenv import load_dotenv

# LangChain imports
from langchain_huggingface import HuggingFaceEmbeddings, ChatHuggingFace, HuggingFaceEndpoint

from langchain_community.vectorstores import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableParallel, RunnablePassthrough, RunnableLambda
from langchain_core.output_parsers import StrOutputParser


def posts_by_category(request, category_id):
    # Fetch the posts that belongs to the category with the id category_id
    posts = Blog.objects.filter(status='Published', category=category_id)
    
    # Use get_object_or_404 when you want to show 404 error page if the category does not exist
    category = get_object_or_404(Category, pk=category_id)
    
    context = {
        'posts': posts,
        'category': category,
    }
    return render(request, 'posts_by_category.html', context)


def blogs(request, slug):

    # Fetch blog
    single_blog = get_object_or_404(Blog, slug=slug, status='Published')

    # Handle comment submission
    if request.method == 'POST':
        comment = Comment()
        comment.user = request.user
        comment.blog = single_blog
        comment.comment = request.POST['comment']
        comment.save()
        return HttpResponseRedirect(request.path_info)

    # Fetch comments
    comments = Comment.objects.filter(blog=single_blog)
    comment_count = comments.count()

    # ----------------------------------------------------------------------
    # RECOMMENDED BLOGS SECTION (LangChain + ChromaDB)
    # ----------------------------------------------------------------------

    # Text of current blog → for embedding
    query_text = f"{single_blog.title}. {single_blog.short_description}"

    # Get all blogs except the current one
    other_blogs = Blog.objects.filter(status='Published').exclude(id=single_blog.id)

    # If less than 3 blogs exist → fallback
    if other_blogs.count() < 3:
        recommended_blogs = other_blogs[:3]
    else:
        # Prepare LangChain documents
        docs = []
        for b in other_blogs:
            content = f"{b.title}. {b.short_description}"
            docs.append(Document(page_content=content, metadata={"id": b.id}))

        # Embeddings
        embeddings = HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )

        # ChromaDB In-memory
        vector_store = Chroma.from_documents(
            documents=docs,
            embedding=embeddings,
            collection_name="blog_recommendations"
        )

        # Get many results (k=10 to avoid missing)
        results = vector_store.similarity_search(query_text, k=10)

        # Extract unique IDs (avoid duplicates)
        recommended_ids = []
        for res in results:
            blog_id = res.metadata["id"]
            if blog_id != single_blog.id and blog_id not in recommended_ids:
                recommended_ids.append(blog_id)

        # Take top 3
        recommended_ids = recommended_ids[:3]

        # If still less than 3 → fill randomly
        if len(recommended_ids) < 3:
            remaining_needed = 3 - len(recommended_ids)
            existing_ids = set(recommended_ids)

            fallback = list(
                other_blogs.exclude(id__in=existing_ids)
            )

            random.shuffle(fallback)
            recommended_ids.extend([b.id for b in fallback[:remaining_needed]])

        # Final recommended blogs
        recommended_blogs = Blog.objects.filter(id__in=recommended_ids)

    # ----------------------------------------------------------------------
    
    print("recommended blogs -> ", recommended_blogs)

    context = {
        'single_blog': single_blog,
        'comments': comments,
        'comment_count': comment_count,
        'recommended_blogs': recommended_blogs,
        'ai_answer' : None
    }

    return render(request, 'blogs.html', context)


def search(request):
    keyword = request.GET.get('keyword')
    
    blogs = Blog.objects.filter(Q(title__icontains=keyword) | Q(short_description__icontains=keyword) | Q(blog_body__icontains=keyword), status='Published')
  
    context = {
        'blogs': blogs,
        'keyword': keyword,
    }
    return render(request, 'search.html', context)


def format_docs(retrieved_docs):
    return "\n\n".join([doc.page_content for doc in retrieved_docs])


def ask_ai_blog(request, slug):
    blog = get_object_or_404(Blog, slug=slug, status='Published')
    ai_answer = None

    if request.method == "POST":
        user_question = request.POST.get("question")

        # --------------------------
        # Build Blog Source Text
        # --------------------------
        full_text = f"""
        Title: {blog.title}
        Short Description: {blog.short_description}
        Blog Content: {blog.blog_body}

        Author: {blog.author}
        Category: {blog.category}
        Created At: {blog.created_at}
        Last Updated: {blog.updated_at}
        """

        # -------------------------------------
        # Chunking for long blog posts
        # -------------------------------------
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=300,
            chunk_overlap=120
        )
        documents = splitter.create_documents([full_text])

        # --------------------------
        # Embeddings + ChromaDB
        # --------------------------
        embeddings = HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"  # smaller but faster model
            # model_name="sentence-transformers/all-mpnet-base-v2" # bigger and heavy model, need GPU and more RAM
        )

        vector_store = Chroma.from_documents(
            documents=documents,
            embedding=embeddings,
            collection_name="blog_rag_store"
        )

        retriever = vector_store.as_retriever(search_kwargs={"k": 12})

        # --------------------------
        # Relevance Check
        # --------------------------
        retrieved = retriever.get_relevant_documents(user_question)

        if len(retrieved) == 0:
            ai_answer = "This question is not relevant with respect to this blog post."
        else:
            # If score/embedding relevance needed:
            # (Chroma returns docs but not score by default — simple check)

            # --------------------------
            # Prompt Template
            # --------------------------
            prompt = PromptTemplate(
                template="""
                You are a helpful assistant.
                Answer ONLY using the blog context below.
                If the user's question is not related to the context,
                respond exactly with:
                "This question is not relevant with respect to this blog post."

                Blog Context:
                {context}

                Question: {question}
                """,
                input_variables=['context', 'question']
            )

            # --------------------------
            # Load GPT-OSS-20B (HF API)
            # --------------------------
            load_dotenv()

            llm = ChatHuggingFace(
                llm=HuggingFaceEndpoint(
                    repo_id="openai/gpt-oss-20b",  
                    temperature=0.2,
                )
            )

            parser = StrOutputParser()

            # --------------------------
            # Pipeline (same as reference)
            # --------------------------
            rag_input = RunnableParallel({
                'context': retriever | RunnableLambda(format_docs),
                'question': RunnablePassthrough()
            })

            chain = rag_input | prompt | llm | parser

            # raw_answer = chain.invoke(user_question)
            
            try:
                raw_answer = chain.invoke(user_question)
                print("AI ASK -> " , raw_answer)
            except Exception as e:
                print("ERROR:", e)
            
            ai_answer = f"""
                        <div>
                            <p><strong>User:</strong> {user_question}</p>
                            <p><strong>AI:</strong> {raw_answer.strip()}</p>
                        </div>
                        """
    
    
    # --------------------------
    # Save chat history in session
    # --------------------------
    history = request.session.get("chat_history", [])

    history.append({
        "question": user_question,
        "answer": raw_answer.strip()
    })

    # Store back into session
    request.session["chat_history"] = history
    
    
    # -------------------------------------
    # Render same blog page with AI answer
    # -------------------------------------
    
    return render(request, "blogs.html", {
        "single_blog": blog,
        "ai_answer": ai_answer,
        "chat_history": request.session.get("chat_history", [])
    })