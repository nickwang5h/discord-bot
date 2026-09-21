"""Explicit topic registry; no import scanning or dynamic plugin framework."""
from core.news.topics.discovery import DiscoveryTopic
from core.news.topics.general import GeneralTopic
from core.news.topics.power_projects import PowerProjectsTopic

TOPICS = {topic.name: topic for topic in (GeneralTopic(), DiscoveryTopic(), PowerProjectsTopic())}
