"""Strict public NAVER contracts and fixed utility request mappings.

The public names are snake_case; only payload() emits the utility's aliases.
Limits are bridge admission limits, not NAVER account quota guarantees.
"""
from datetime import date, timedelta
from calendar import monthrange
from typing import Annotated, Literal
import re
from pydantic import BaseModel, ConfigDict, Field, AfterValidator, model_validator


def clean(value):
    value = ' '.join(value.split())
    if not value:
        raise ValueError('blank text')
    return value

Text = Annotated[str, Field(strict=True, min_length=1, max_length=100), AfterValidator(clean)]
Query = Annotated[str, Field(strict=True, min_length=1, max_length=200), AfterValidator(clean)]
Code = Annotated[str, Field(strict=True, min_length=1, max_length=32), AfterValidator(clean)]
Display = Annotated[int, Field(strict=True, ge=1, le=30)]
Start = Annotated[int, Field(strict=True, ge=1, le=1000)]
Device = Literal['pc', 'mo']
Gender = Literal['m', 'f']
SearchAge = Literal['1','2','3','4','5','6','7','8','9','10','11']
ShoppingAge = Literal['10','20','30','40','50','60']

class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')

class QueryOnly(Contract):
    query: Query

class Search(QueryOnly):
    display: Display = 10
    start: Start = 1

class SortedSearch(Search):
    sort: Literal['sim', 'date'] = 'sim'

class KinSearch(Search):
    sort: Literal['sim', 'date', 'point'] = 'sim'

class ImageSearch(SortedSearch):
    filter: Literal['all', 'large', 'medium', 'small'] = 'all'

class LocalSearch(QueryOnly):
    display: Annotated[int, Field(strict=True, ge=1, le=5)] = 1
    sort: Literal['random', 'comment'] = 'random'

class KeywordGroup(Contract):
    group_name: Text
    keywords: Annotated[list[Text], Field(min_length=1, max_length=20)]

class CategoryPair(Contract):
    name: Text
    param: Annotated[list[Code], Field(min_length=1, max_length=5)]

class KeywordPair(Contract):
    name: Text
    param: Annotated[list[Text], Field(min_length=1, max_length=1)]


def iso(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('invalid date')
    date.fromisoformat(value)
    return value

ISODate = Annotated[str, Field(strict=True, min_length=10, max_length=10), AfterValidator(iso)]

def bucket_bounds(start, end, unit):
    """Inclusive calendar coverage; not clipped to request or provider minimum."""
    a, b = date.fromisoformat(iso(start)), date.fromisoformat(iso(end))
    if unit == 'week':
        a, b = a-timedelta(days=a.weekday()), b+timedelta(days=6-b.weekday())
    elif unit == 'month':
        a, b = a.replace(day=1), b.replace(day=monthrange(b.year,b.month)[1])
    elif unit != 'date':
        raise ValueError('invalid time unit')
    return a.isoformat(), b.isoformat()


def valid_envelope(start, end, unit, provider_start, provider_end, provider_unit):
    """Permit whole exact-request OR whole normalized pair, never mixed bounds."""
    if provider_unit != unit or type(provider_start) is not str or type(provider_end) is not str:
        return False
    try:
        iso(provider_start); iso(provider_end)
        return (provider_start,provider_end) in {(start,end),bucket_bounds(start,end,unit)}
    except (ValueError, TypeError, OverflowError):
        return False


def periods(start, end, unit):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if unit == 'month':
        return (b.year-a.year)*12+b.month-a.month+1
    if unit == 'week':
        # Count calendar week buckets, including both partial boundary weeks.
        return ((b.toordinal()-b.weekday())-(a.toordinal()-a.weekday()))//7+1
    return (b-a).days+1

class Trend(Contract):
    start_date: ISODate
    end_date: ISODate
    time_unit: Literal['date','week','month']
    device: Device | None = None
    gender: Gender | None = None

    @model_validator(mode='after')
    def bounds(self):
        minimum = '2016-01-01' if isinstance(self, KeywordTrend) else '2017-08-01'
        if self.start_date < minimum or self.end_date < self.start_date:
            raise ValueError('invalid date range')
        if periods(self.start_date, self.end_date, self.time_unit) > 366:
            raise ValueError('maximum 366 periods per series')
        return self

class KeywordTrend(Trend):
    keyword_groups: Annotated[list[KeywordGroup], Field(min_length=1, max_length=5)]
    ages: Annotated[list[SearchAge], Field(max_length=11)] | None = None

class ShoppingTrend(Trend):
    ages: Annotated[list[ShoppingAge], Field(max_length=6)] | None = None

class CategoriesTrend(ShoppingTrend):
    category: Annotated[list[CategoryPair], Field(min_length=1, max_length=3)]

class CategoryTrend(ShoppingTrend):
    category: Code

class KeywordsTrend(CategoryTrend):
    keyword: Annotated[list[KeywordPair], Field(min_length=1, max_length=5)]

class KeywordDetailTrend(CategoryTrend):
    keyword: Text

# name -> (request model, fixed route, source or mode)
OPERATIONS = {
    'naver_search_web': (Search, '/naver/search', 'webkr'),
    'naver_search_encyclopedia': (Search, '/naver/search', 'encyc'),
    'naver_search_news': (SortedSearch, '/naver/search', 'news'),
    'naver_search_blog': (SortedSearch, '/naver/search', 'blog'),
    'naver_search_cafe': (SortedSearch, '/naver/search', 'cafearticle'),
    'naver_search_kin': (KinSearch, '/naver/search', 'kin'),
    'naver_search_local': (LocalSearch, '/naver/search', 'local'),
    'naver_search_image': (ImageSearch, '/naver/search', 'image'),
    'naver_convert_keyboard_query': (QueryOnly, '/naver/search', 'errata'),
    'naver_check_adult_query': (QueryOnly, '/naver/search', 'adult'),
    'naver_keyword_trend': (KeywordTrend, '/naver/keyword-trend', None),
    'naver_shopping_category_trend': (CategoriesTrend, '/naver/shopping-trend', 'categories'),
    'naver_shopping_keyword_trend': (KeywordsTrend, '/naver/shopping-trend', 'keywords'),
}
for dimension in ('device','gender','age'):
    OPERATIONS['naver_shopping_category_'+dimension+'_trend'] = (CategoryTrend, '/naver/shopping-trend', 'category_'+dimension)
    OPERATIONS['naver_shopping_keyword_'+dimension+'_trend'] = (KeywordDetailTrend, '/naver/shopping-trend', 'keyword_'+dimension)


def payload(operation, arguments):
    model, route, discriminator = OPERATIONS[operation]
    obj = model.model_validate(arguments, strict=True).model_dump(exclude_none=True)
    if route == '/naver/search':
        obj['source'] = discriminator
        if discriminator == 'local':
            obj['start'] = 1
    else:
        for public, private in (('start_date','startDate'), ('end_date','endDate'), ('time_unit','timeUnit'), ('keyword_groups','keywordGroups')):
            if public in obj:
                obj[private] = obj.pop(public)
        for group in obj.get('keywordGroups', []):
            group['groupName'] = group.pop('group_name')
        if discriminator:
            obj['mode'] = discriminator
    return route, obj
