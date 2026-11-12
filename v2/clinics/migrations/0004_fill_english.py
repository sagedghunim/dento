# Fill the English text of rows copied from the catalogues before design G3 (sections, services, medicines,
# prescription templates, procedure descriptions, stock items, expense categories). Only empty English columns of rows
# that still hold the catalogue's Arabic text are filled; nothing typed is changed. The maps are frozen here, as the
# catalogue was when this migration was written.

from django.db import migrations

SECTIONS = {'أسنان': 'Dental',
 'أطفال': 'Pediatrics',
 'أنف وأذن وحنجرة': 'Ear, nose & throat',
 'باطنة': 'Internal medicine',
 'بيطري': 'Veterinary',
 'جلدية وتجميل': 'Dermatology & cosmetic',
 'رمد': 'Ophthalmology',
 'عظام': 'Orthopaedics',
 'علاج طبيعي': 'Physiotherapy',
 'قلب': 'Cardiology',
 'نسا وتوليد': 'Obstetrics & gynaecology'}
SERVICES = {'إعادة كشف': 'Follow-up',
 'إيكو': 'Echocardiography',
 'تبييض': 'Whitening',
 'تطعيم': 'Vaccination',
 'تقييم': 'Assessment',
 'تنظيف جير': 'Cleaning',
 'جبيرة': 'Cast',
 'جلسة تقشير': 'Peeling session',
 'جلسة علاج': 'Treatment session',
 'جلسة ليزر': 'Laser session',
 'حشو عادي': 'Filling',
 'خلع': 'Extraction',
 'رسم قلب': 'ECG',
 'زراعة': 'Implant',
 'سونار': 'Ultrasound',
 'علاج عصب': 'Root canal',
 'قياس نظر': 'Vision test',
 'كشف': 'Consultation',
 'متابعة': 'Follow-up',
 'متابعة حمل': 'Antenatal visit',
 'متابعة سكر': 'Diabetes follow-up',
 'متابعة نمو': 'Growth check',
 'منظار': 'Endoscopy'}
TEXT = {'1 مل 4 مرات يومياً': '1 ml four times a day',
 '4 نقط 3 مرات يومياً': '4 drops three times a day',
 '4 نقط مرتين يومياً لمدة 7 أيام': '4 drops twice a day for 7 days',
 '5 مل 3 مرات يومياً': '5 ml three times a day',
 'أقراص': 'Tablets',
 'أقراص فوارة': 'Effervescent tablets',
 'أكياس': 'Sachets',
 'اضغط على القطنة نص ساعة. لا مضمضة ولا بصق 24 ساعة. أكل بارد وطري أول يوم. ممنوع التدخين والشفاط 3 أيام.': 'Bite on '
                                                                                                            'the '
                                                                                                            'gauze '
                                                                                                            'for '
                                                                                                            'half an '
                                                                                                            'hour. '
                                                                                                            'No '
                                                                                                            'rinsing '
                                                                                                            'or '
                                                                                                            'spitting '
                                                                                                            'for 24 '
                                                                                                            'hours. '
                                                                                                            'Cold, '
                                                                                                            'soft '
                                                                                                            'food on '
                                                                                                            'the '
                                                                                                            'first '
                                                                                                            'day. No '
                                                                                                            'smoking '
                                                                                                            'or '
                                                                                                            'drinking '
                                                                                                            'through '
                                                                                                            'a straw '
                                                                                                            'for 3 '
                                                                                                            'days.',
 'بالنيبولايزر': 'With a nebuliser',
 'بحد أقصى 8 أقراص يومياً': 'No more than 8 tablets a day',
 'بخاخ': 'Spray',
 'بخاخ أنف': 'Nasal spray',
 'بخة في كل فتحة مرتين يومياً': 'One spray in each nostril twice a day',
 'بختين في كل فتحة مرة يومياً': 'Two sprays in each nostril once a day',
 'بدون كحوليات': 'No alcohol',
 'بعد الأكل': 'After meals',
 'بعد الأكل، بحد أقصى 5 أيام': 'After meals, for 5 days at most',
 'بعد الأكل، بدون كحوليات': 'After meals, no alcohol',
 'بعد الجراحة': 'After surgery',
 'بعد الخلع الجراحي / ضرس العقل': 'After surgical extraction / wisdom tooth',
 'بعد الخلع العادي': 'After a simple extraction',
 'بعد تنظيف الأسنان': 'After brushing the teeth',
 'بعد علاج العصب': 'After root canal treatment',
 'تكرر بعد 3 شهور': 'Repeat after 3 months',
 'تنظيف الأسنان مرتين يومياً': 'Brush the teeth twice a day',
 'توسيع حدقة': 'Dilates the pupil',
 'جل': 'Gel',
 'جل للفم': 'Oral gel',
 'حسب العمر مرة يومياً': 'By age, once a day',
 'حسب المدة المحددة': 'For the prescribed duration',
 'حسب الوزن': 'By weight',
 'حسب الوزن 3 مرات يومياً': 'By weight, three times a day',
 'حسب الوزن جرعة واحدة': 'By weight, a single dose',
 'حسب الوزن كل 12 ساعة لمدة 7 أيام': 'By weight, every 12 hours for 7 days',
 'حسب الوزن كل 6 ساعات عند اللزوم': 'By weight, every 6 hours as needed',
 'حسب الوزن كل 8 ساعات عند اللزوم': 'By weight, every 8 hours as needed',
 'حسب الوزن مرة يومياً': 'By weight, once a day',
 'حسب الوزن مرة يومياً لمدة 3 أيام': 'By weight, once a day for 3 days',
 'حسب الوزن مرتين يومياً': 'By weight, twice a day',
 'حسب تعليمات الطبيب': 'As the doctor directs',
 'حسب نتيجة السيولة': 'According to the INR result',
 'حقن': 'Injection',
 'خراج / التهاب حاد': 'Abscess / acute infection',
 'خراج مع حساسية البنسلين': 'Abscess with penicillin allergy',
 'شراب': 'Syrup',
 'على الجلد': 'On the skin',
 'على مناطق محدودة': 'On small areas only',
 'غرغرة': 'Gargle',
 'غرغرة 3 مرات يومياً': 'Gargle three times a day',
 'غسول': 'Wash',
 'فطريات في الفم': 'Oral thrush',
 'قبل الأكل': 'Before meals',
 'قرحة في الفم': 'Mouth ulcer',
 'قرص 3 مرات يومياً': 'One tablet three times a day',
 'قرص 3 مرات يومياً عند اللزوم': 'One tablet three times a day as needed',
 'قرص 3 مرات يومياً لمدة 3 أيام': 'One tablet three times a day for 3 days',
 'قرص أو اتنين كل 6 ساعات': 'One or two tablets every 6 hours',
 'قرص أو اتنين كل 6 ساعات عند اللزوم': 'One or two tablets every 6 hours as needed',
 'قرص صباحاً': 'One tablet in the morning',
 'قرص قبل الأكل 3 مرات': 'One tablet before meals, three times a day',
 'قرص قبل الأكل عند اللزوم': 'One tablet before meals as needed',
 'قرص قبل الفطار': 'One tablet before breakfast',
 'قرص كل 12 ساعة عند اللزوم': 'One tablet every 12 hours as needed',
 'قرص كل 12 ساعة لمدة 5 أيام': 'One tablet every 12 hours for 5 days',
 'قرص كل 12 ساعة لمدة 7 أيام': 'One tablet every 12 hours for 7 days',
 'قرص كل 8 ساعات': 'One tablet every 8 hours',
 'قرص كل 8 ساعات عند اللزوم': 'One tablet every 8 hours as needed',
 'قرص كل 8 ساعات لمدة 5 أيام': 'One tablet every 8 hours for 5 days',
 'قرص مرة يومياً': 'One tablet once a day',
 'قرص مرة يومياً بعد الأكل': 'One tablet once a day after meals',
 'قرص مرة يومياً قبل الفطار': 'One tablet once a day before breakfast',
 'قرص مرة يومياً لمدة 3 أيام': 'One tablet once a day for 3 days',
 'قرص مرة يومياً مساءً': 'One tablet once a day in the evening',
 'قرص مرتين يومياً': 'One tablet twice a day',
 'قرص مرتين يومياً بعد الأكل': 'One tablet twice a day after meals',
 'قرص مرتين يومياً مع الأكل': 'One tablet twice a day with meals',
 'قرص مساءً': 'One tablet in the evening',
 'قرص يومياً': 'One tablet a day',
 'قرص يومياً بعد الغداء': 'One tablet a day after lunch',
 'قرصين 3 مرات يومياً': 'Two tablets three times a day',
 'قطرة': 'Drops',
 'كبسول': 'Capsules',
 'كبسولة كل 8 ساعات لمدة 5 أيام': 'One capsule every 8 hours for 5 days',
 'كبسولة مرة أو مرتين يومياً': 'One capsule once or twice a day',
 'كبسولة مرتين يومياً': 'One capsule twice a day',
 'كبسولة يومياً بعد الأكل': 'One capsule a day after meals',
 'كريم': 'Cream',
 'كمادات ثلج أول يوم. لا مضمضة 24 ساعة. ممنوع التدخين.': 'Ice packs on the first day. No rinsing for 24 hours. No '
                                                         'smoking.',
 'كيس في 200 مل ماء بعد كل إسهال': 'One sachet in 200 ml of water after each loose stool',
 'لا تأكل على الناحية دي لحد التاج أو الحشو النهائي.': "Don't chew on this side until the crown or the final "
                                                       'filling.',
 'لا يزيد عن 5 أيام': 'No longer than 5 days',
 'لا يزيد عن أسبوعين': 'No longer than two weeks',
 'لبوس': 'Suppositories',
 'لبوس مهبلي': 'Vaginal suppositories',
 'لبوسة مساءً': 'One suppository at night',
 'لبوسة مساءً لمدة 3 أيام': 'One suppository at night for 3 days',
 'لحساسية البنسلين': 'For penicillin allergy',
 'لحماية المعدة مع المسكنات': 'Protects the stomach with painkillers',
 'ليس لكل السلالات': 'Not for every breed',
 'متابعة INR': 'Monitor the INR',
 'محلول': 'Solution',
 'محلول بخار': 'Nebuliser solution',
 'محلول وريد': 'IV solution',
 'مضمضة 10 مل لمدة دقيقة مرتين يومياً': 'Rinse with 10 ml for one minute twice a day',
 'مضمضة 3 مرات يومياً': 'Rinse three times a day',
 'مضمضة مرتين يومياً': 'Rinse twice a day',
 'مع المسكنات لحماية المعدة': 'With painkillers, to protect the stomach',
 'معجون': 'Toothpaste',
 'معجون للفم': 'Oral paste',
 'معلق': 'Suspension',
 'نقط': 'Drops',
 'نقط أذن': 'Ear drops',
 'نقط أنف': 'Nasal drops',
 'نقط مرتين يومياً': 'Drops twice a day',
 'نقطة 3 مرات يومياً لمدة أسبوع': 'One drop three times a day for a week',
 'نقطة 4 مرات يومياً': 'One drop four times a day',
 'نقطة 4 مرات يومياً لمدة أسبوع': 'One drop four times a day for a week',
 'نقطة عند اللزوم': 'One drop as needed',
 'نقطة قبل الفحص': 'One drop before the examination',
 'نقطة مرتين يومياً': 'One drop twice a day',
 'نقطة مساءً': 'One drop at night',
 'نقطتين في كل فتحة عند اللزوم': 'Two drops in each nostril as needed',
 'يبدأ بعد 24 ساعة من الخلع': 'Start 24 hours after the extraction',
 'يوضع صباحاً ويُكرر كل ساعتين في الشمس': 'Apply in the morning and every two hours in the sun',
 'يوضع على القرحة 2–3 مرات يومياً': 'Apply to the ulcer 2 to 3 times a day',
 'يوضع على المكان 4 مرات يومياً': 'Apply to the area four times a day',
 'يُترك في الفم قبل البلع': 'Hold in the mouth before swallowing',
 'يُدهن 2–3 مرات يومياً': 'Apply 2 to 3 times a day',
 'يُدهن 3 مرات يومياً': 'Apply three times a day',
 'يُدهن طبقة رفيعة مرتين يومياً': 'Apply a thin layer twice a day',
 'يُدهن على قرحة البرد 5 مرات يومياً لمدة 5 أيام': 'Apply to the cold sore five times a day for 5 days',
 'يُدهن مرة يومياً': 'Apply once a day',
 'يُدهن مرتين يومياً': 'Apply twice a day',
 'يُدهن مرتين يومياً لمدة 7 أيام': 'Apply twice a day for 7 days',
 'يُدهن مرتين يومياً لمدة أسبوعين': 'Apply twice a day for two weeks',
 'يُدهن مساءً على البشرة الجافة': 'Apply at night on dry skin'}
DESCRIPTIONS = {'أشعة ثلاثية الأبعاد لفك واحد، قبل الزراعة أو الجراحة.': 'A 3D scan of one jaw, before an implant or surgery.',
 'إزالة الجزء العلوي من العصب وتغطية الباقي، غالباً للأطفال.': 'Removing the top of the nerve and covering the rest, '
                                                               'usually for children.',
 'إزالة الجير والتصبغات فوق اللثة ثم تلميع الأسنان.': 'Removal of tartar and stains above the gum line, then '
                                                      'polishing.',
 'إزالة حشو عصب قديم فاشل وإعادة العلاج.': 'Removing an old failed root filling and treating the canals again.',
 'إصلاح كسر في الطقم أو إضافة سنة له.': 'Repairing a broken denture or adding a tooth to it.',
 'إضافة استخدام أدوات الروتاري في علاج العصب.': 'The added use of rotary files in root canal treatment.',
 'إظهار جزء أكبر من السنة جراحياً قبل التلبيسة.': 'Surgically exposing more of the tooth before a crown.',
 'إعادة لصق تلبيسة سليمة خرجت من مكانها.': 'Re-cementing a sound crown that came off.',
 'بناء أو إصلاح سنة أمامية مكسورة بالحشو التجميلي.': 'Rebuilding or repairing a broken front tooth with composite.',
 'بناء جسم السنة المفقود قبل التلبيسة.': 'Rebuilding the missing body of the tooth before a crown.',
 'تاج معدني جاهز لضرس لبني متآكل.': 'A ready-made metal crown for a worn baby molar.',
 'تبطين الطقم ليركب على اللثة من جديد.': 'Relining a denture so it fits the gums again.',
 'تبييض الأسنان في العيادة بالجل والليزر في جلسة واحدة.': 'Whitening in the clinic with gel and laser in one '
                                                          'session.',
 'تركيب الدعامة والتاج فوق الزرعة بعد التحامها.': 'Fitting the abutment and crown on the implant after it has healed '
                                                  'in.',
 'تركيب جذر الزرعة داخل عظم الفك.': 'Placing the implant root in the jawbone.',
 'تقويم ثابت بالبراكيت الشفاف للحالة كاملة.': 'Fixed clear braces for the whole case.',
 'تقويم ثابت بالبراكيت المعدني للحالة كاملة، والمتابعة تُحسب على حدة.': 'Fixed metal braces for the whole case; '
                                                                        'follow-up visits are charged separately.',
 'تقييم إطباق الأسنان والفكين وشرح خيارات التقويم ومدته.': 'Assessment of the bite and jaws, with the orthodontic '
                                                           'options and how long they take.',
 'تلبيسة إي ماكس شفافة للأسنان الأمامية.': 'A translucent E-max crown for front teeth.',
 'تلبيسة بورسلين على قاعدة معدنية.': 'A porcelain crown on a metal base.',
 'تلبيسة زركون قوية بلون طبيعي.': 'A strong zirconia crown in a natural shade.',
 'تلبيسة مؤقتة لحين تركيب الدائمة.': 'A temporary crown until the permanent one is fitted.',
 'تنظيف تحت اللثة وتنعيم أسطح الجذور لربع واحد من الفم.': 'Cleaning below the gum and smoothing the roots in one '
                                                          'quarter of the mouth.',
 'تنظيف وتشكيل وحشو قناة عصب واحدة.': 'Cleaning, shaping and filling one root canal.',
 'تنظيف وتشكيل وحشو قناتين.': 'Cleaning, shaping and filling two canals.',
 'جهاز يحفظ مكان سنة لبنية خُلعت مبكراً.': 'An appliance that keeps the space of a baby tooth lost early.',
 'حشو تجميلي بلون السنة لسطح واحد بعد إزالة التسوس.': 'A tooth-coloured filling on one surface after the decay is '
                                                      'removed.',
 'حشو تجميلي بلون السنة يغطي سطحين.': 'A tooth-coloured filling covering two surfaces.',
 'حشو تجميلي واسع يغطي ثلاثة أسطح أو أكثر.': 'A large tooth-coloured filling covering three or more surfaces.',
 'حشو مؤقت بين الجلسات أو لتهدئة السنة.': 'A temporary filling between sessions or to settle the tooth.',
 'حشو مصنوع في المعمل من البورسلين أو الزركون ويُلصق على السنة.': 'A lab-made porcelain or zirconia filling bonded '
                                                                  'to the tooth.',
 'حشو معدني فضي للأضراس الخلفية.': 'A silver metal filling for back teeth.',
 'حشو يطلق الفلورايد، مناسب للأطفال وعنق السنة.': 'A fluoride-releasing filling, suited to children and the neck of '
                                                  'the tooth.',
 'خلع جراحي لسنة مكسورة أو جذور متبقية.': 'Surgical removal of a broken tooth or remaining roots.',
 'خلع سنة ظاهرة بدون جراحة.': 'Removing a visible tooth without surgery.',
 'خلع سنة لبنية للطفل.': "Removing a child's baby tooth.",
 'خلع ضرس عقل مدفون أو مائل جراحياً.': 'Surgical removal of a buried or tilted wisdom tooth.',
 'دهان أو جل فلورايد لتقوية المينا والوقاية من التسوس.': 'Fluoride varnish or gel to strengthen the enamel and '
                                                         'prevent decay.',
 'رفع غشاء الجيب الأنفي وإضافة عظم قبل زراعة الأضراس العلوية.': 'Lifting the sinus membrane and adding bone before '
                                                                'upper back implants.',
 'زيارة متابعة التقويم الشهرية وتغيير السلك أو الأستك.': 'The monthly braces visit to change the wire or elastics.',
 'سد شقوق سطح الضرس بمادة سائلة تتصلب، للوقاية.': 'Sealing the grooves of a molar with a liquid that hardens, as '
                                                  'prevention.',
 'صورة صغيرة لسنة أو اثنتين تُظهر الجذر والعظم حوله.': 'A small film of one or two teeth showing the root and the '
                                                       'bone around it.',
 'صورة للأضراس العلوية والسفلية معاً لكشف التسوس بين الأسنان.': 'A film of the upper and lower back teeth together, '
                                                                'to find decay between teeth.',
 'صورة واحدة لكل الأسنان والفكين والمفصل.': 'One image of all the teeth, both jaws and the joints.',
 'طقم جزئي بهيكل معدني أخف وأقوى.': 'A partial denture on a lighter, stronger metal frame.',
 'طقم جزئي متحرك من الأكريليك.': 'A removable partial denture in acrylic.',
 'طقم جزئي مرن بدون خطافات معدنية ظاهرة.': 'A flexible partial denture with no visible metal clasps.',
 'طقم كامل متحرك لفك واحد.': 'A full removable denture for one jaw.',
 'علاج جيوب اللثة بالليزر لربع واحد، بعد التنظيف العميق.': 'Laser treatment of gum pockets in one quarter, after '
                                                           'deep scaling.',
 'علاج عصب ضرس بثلاث قنوات أو أكثر.': 'Root canal treatment of a molar with three or more canals.',
 'علاج عصب كامل لسنة لبنية.': 'Full root canal treatment of a baby tooth.',
 'فتح الخراج وتصريف الصديد وتنظيف المكان.': 'Opening the abscess, draining the pus and cleaning the area.',
 'فتح العصب لتخفيف الألم ووضع حشو دوائي مؤقت.': 'Opening the nerve to relieve pain and placing a temporary medicated '
                                                'dressing.',
 'فتح اللثة جراحياً لتنظيف الجذور والعظم ثم خياطتها.': 'The gum is opened to clean the roots and bone, then '
                                                       'stitched.',
 'فحص الفم والأسنان واللثة وتسجيل الحالة وخطة العلاج المبدئية.': 'Examination of the mouth, teeth and gums, a '
                                                                 'charted record and a first treatment plan.',
 'قشرة تجميلية من الحشو تُبنى مباشرة على السنة.': 'A cosmetic composite shell built directly on the tooth.',
 'قشرة رقيقة من البورسلين تُلصق على وجه السنة الأمامية.': 'A thin porcelain shell bonded to the front of a tooth.',
 'قص اللثة الزائدة أو الملتهبة حول سنة واحدة.': 'Trimming excess or inflamed gum around one tooth.',
 'قطع طرف الجذر جراحياً وتنظيف الالتهاب حوله.': 'Surgical removal of the root tip and cleaning of the infection '
                                                'around it.',
 'قوالب مفصّلة وجل تبييض يستخدمه المريض في البيت.': 'Custom trays and whitening gel the patient uses at home.',
 'كشف رأس الزرعة بعد فترة الالتحام وتركيب مشكّل اللثة.': 'Exposing the implant head after healing and fitting a gum '
                                                         'former.',
 'مثبت بعد التقويم، شفاف أو سلك ثابت، لفك واحد.': 'A retainer after braces, clear or a fixed wire, for one jaw.',
 'واقٍ شفاف يُلبس ليلاً لحماية الأسنان من الصرير.': 'A clear guard worn at night to protect the teeth from grinding.',
 'واقٍ مفصّل على الأسنان للرياضات العنيفة.': 'A custom guard for contact sports.',
 'وتد فايبر داخل جذر معالج وبناء فوقه لحمل التلبيسة.': 'A fibre post in a treated root with a core built on it to '
                                                       'hold a crown.',
 'وحدة واحدة من كوبري ثابت يعوض سنة مفقودة.': 'One unit of a fixed bridge replacing a missing tooth.',
 'وضع عظم صناعي أو طبيعي لتعويض نقص العظم في مكان واحد.': 'Synthetic or natural bone placed to make up bone loss at '
                                                          'one site.'}
ITEMS = {'بنج (كارتريدج)': 'Local anaesthetic (cartridge)',
 'بوست فايبر': 'Fibre post',
 'جلاس أيونومر (كبسولة)': 'Glass ionomer (capsule)',
 'جوانتي (زوج)': 'Gloves (pair)',
 'جوتا بيركا (قمع)': 'Gutta-percha point',
 'خيط جراحي': 'Suture',
 'سن بنج': 'Dental needle',
 'شاش معقم (عبوة)': 'Sterile gauze (pack)',
 'كومبوزيت (كبسولة)': 'Composite (compule)',
 'ماسك طبي': 'Face mask',
 'مبرد روتاري': 'Rotary file'}
CATEGORIES = {'lab': 'Lab',
 'materials': 'Materials',
 'other': 'Other',
 'rent': 'Rent',
 'salaries': 'Salaries',
 'utilities': 'Utilities'}

# (table, key column, English column, map)
FILLS = [
    ("clinics_section", "name", "name_en", SECTIONS),
    ("clinics_service", "name", "name_en", SERVICES),
    ("clinical_medicine", "form", "form_en", TEXT),
    ("clinical_medicine", "default_dose", "default_dose_en", TEXT),
    ("clinical_medicine", "default_note", "default_note_en", TEXT),
    ("clinical_prescription_template", "name", "name_en", TEXT),
    ("clinical_prescription_template", "notes", "notes_en", TEXT),
    ("clinical_procedure", "description", "description_en", DESCRIPTIONS),
    ("inventory_item", "name", "name_en", ITEMS),
    ("finance_category", "code", "name_en", CATEGORIES),
]


def fill(apps, schema_editor):
    """Row-level security is forced even for the owner and a migration has no clinic set, so FORCE is lifted for
    this transaction (as in clinics 0002)."""
    with schema_editor.connection.cursor() as cursor:
        for table in sorted({f[0] for f in FILLS}):
            cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        for table, key, column, mapping in FILLS:
            pairs = [(k, v) for k, v in mapping.items() if k and v]
            values = ", ".join(["(%s, %s)"] * len(pairs))
            cursor.execute(f'UPDATE "{table}" t SET {column} = m.en FROM (VALUES {values}) AS m(ar, en) '
                           f"WHERE t.{key} = m.ar AND t.{column} = ''", [p for pair in pairs for p in pair])
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        for table in sorted({f[0] for f in FILLS}):
            cursor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


class Migration(migrations.Migration):

    dependencies = [
        ("clinics", "0003_team_and_english_names"),
        ("clinical", "0004_english_text"),
        ("inventory", "0002_item_name_en"),
        ("finance", "0002_category_name_en"),
    ]

    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
