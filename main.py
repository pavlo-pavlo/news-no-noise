import yaml
import feedparser

with open("sources.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

total_news = []

for category, sources in config["sources"].items():

    print(f"\n=== {category.upper()} ===")

    for source in sources:

        try:
            feed = feedparser.parse(source["url"])

            print(f"\nИсточник: {source['name']}")

            count = 0

            for entry in feed.entries[:5]:

                title = entry.get("title", "")

                link = entry.get("link", "")

                print(f"- {title}")
                print(f"  {link}")

                total_news.append(
                    {
                        "category": category,
                        "source": source["name"],
                        "title": title,
                        "link": link,
                    }
                )

                count += 1

            print(f"Новостей найдено: {count}")

        except Exception as e:

            print(f"Ошибка {source['name']}: {e}")

print("\n====================")
print(f"Всего новостей: {len(total_news)}")
print("====================")
